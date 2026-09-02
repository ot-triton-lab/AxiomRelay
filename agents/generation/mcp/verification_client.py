"""Fail-closed verification client and atomic blueprint promotion."""

from __future__ import annotations

import ctypes
import errno
import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Mapping
from urllib.parse import urlsplit, urlunsplit

import requests

try:
    from .publication_proof_context_v3 import (
        ADAPTIVE_AGGREGATE_CONTEXT_SCHEMA_VERSION,
        AGGREGATE_CONTEXT_SCHEMA_VERSION,
        PROOF_CONTEXT_SCHEMA_VERSION,
        PROOF_ITEM_SCHEMA_VERSION,
        ProofManifest,
        aggregate_adaptive_context_digest,
        aggregate_context_digest,
        build_item_context,
        extract_verification_target,
        parse_blueprint,
    )
except ImportError:  # pragma: no cover - direct module execution
    from publication_proof_context_v3 import (  # type: ignore[no-redef]
        ADAPTIVE_AGGREGATE_CONTEXT_SCHEMA_VERSION,
        AGGREGATE_CONTEXT_SCHEMA_VERSION,
        PROOF_CONTEXT_SCHEMA_VERSION,
        PROOF_ITEM_SCHEMA_VERSION,
        ProofManifest,
        aggregate_adaptive_context_digest,
        aggregate_context_digest,
        build_item_context,
        extract_verification_target,
        parse_blueprint,
    )


_OUTPUT_FIELDS = {
    "output_schema_version",
    "verification_report",
    "verification_status",
    "verdict",
    "repair_hints",
    "needs_expanded_proofs",
    "checked_item_ids",
    "proof_digest",
    "context_digest",
    "adaptive_context_digest",
    "item_context_attestations",
    "verification_attempt_id",
    "verifier_run_id",
    "verifier_model",
    "verifier_reasoning_effort",
    "verifier_service_version",
    "verification_pass_index",
    "verification_role",
}
_REPORT_FIELDS = {"summary", "critical_errors", "gaps"}
_FINDING_FIELDS = {"location", "issue"}
_ITEM_ID_RE = re.compile(r"^pi_[0-9a-f]{24}$")
_HEX_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_VERIFICATION_ATTEMPT_RE = re.compile(r"^veratt_[0-9a-f]{32}$")
_VERIFIER_RUN_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,255}$")
_VERIFICATION_CALLER_RE = re.compile(r"^vcaller_[0-9a-f]{32}$")
_VERIFIER_PASS_IDENTITY_SCHEMA = "rethlas_verifier_pass_identity_v1"
_DIRECT_FINALIZATION_INTENT_SCHEMA_LEGACY = (
    "rethlas_direct_finalization_intent_v1"
)
_DIRECT_FINALIZATION_INTENT_SCHEMA = "rethlas_direct_finalization_intent_v2"
_DIRECT_FINALIZATION_DISPATCH_SCHEMA = "rethlas_direct_finalization_dispatch_v1"
_DIRECT_FINALIZATION_RESULT_SCHEMA_LEGACY = (
    "rethlas_direct_finalization_result_v1"
)
_DIRECT_FINALIZATION_RESULT_SCHEMA = "rethlas_direct_finalization_result_v2"
_DIRECT_FINALIZATION_LEGACY_SNAPSHOT_SCHEMA = (
    "rethlas_direct_finalization_legacy_snapshot_v1"
)
_PREPARED_PUBLICATION_SETTLEMENT_SCHEMA = (
    "rethlas_prepared_publication_settlement_v1"
)
_PREPARED_PUBLICATION_ARCHIVE_SCHEMA_LEGACY = (
    "rethlas_prepared_publication_archive_v1"
)
_PREPARED_PUBLICATION_ARCHIVE_SCHEMA = (
    "rethlas_prepared_publication_archive_v2"
)
_PUBLICATION_ADMISSION_SCHEMA_LEGACY = "rethlas_publication_admission_v1"
_PUBLICATION_ADMISSION_SCHEMA_PREVIOUS = "rethlas_publication_admission_v2"
_PUBLICATION_ADMISSION_SCHEMA_VERIFIER_IDENTITY = "rethlas_publication_admission_v3"
_PUBLICATION_ADMISSION_SCHEMA = "rethlas_publication_admission_v4"
_PUBLICATION_RECOVERY_CERTIFICATE_SCHEMA = (
    "rethlas_cross_layer_publication_recovery_certificate_v1"
)
_OUTER_PUBLICATION_RECOVERY_AUTHORITY_SCHEMA = (
    "rethlas_outer_publication_recovery_authority_v1"
)
_PUBLICATION_RECOVERY_BLUEPRINT_SCHEMA = (
    "rethlas_publication_recovery_blueprint_v1"
)
_RETRYABLE_PUBLICATION_ADMISSION_SETTLEMENTS = frozenset(
    {
        "direct_operational_nonpublication",
        "predispatch_abandoned",
        "prepared_request_drift",
        "external_operational_nonpublication",
    }
)
_RECEIPT_COLLISION_ROLLBACK_SCHEMA = (
    "rethlas_receipt_collision_rollback_intent_v1"
)
_TARGETED_VERIFICATION_INTENT_SCHEMA = (
    "rethlas_targeted_verification_intent_v2"
)
_TARGETED_VERIFICATION_DISPATCH_SCHEMA = (
    "rethlas_targeted_verification_dispatch_v1"
)
_TARGETED_VERIFICATION_RESULT_SCHEMA = (
    "rethlas_targeted_verification_result_v1"
)
_TARGETED_STATUS_TERMINAL_SCHEMA = (
    "rethlas_targeted_verification_status_terminal_v2"
)
_TARGETED_STATUS_PENDING_SCHEMA = "rethlas_targeted_verification_status_pending_v2"
_MAX_DIRECT_FINALIZATION_INTENT_BYTES = 131_072
_ABSOLUTE_MAX_DIRECT_FINALIZATION_INTENT_BYTES = 8_000_000
_MAX_DIRECT_FINALIZATION_RESULT_BYTES = 1_048_576
_MAX_DIRECT_FINALIZATION_REPAIR_HINT_BYTES = 131_072
_MAX_DIRECT_FINALIZATION_SUMMARY_BYTES = 65_536
_ABSOLUTE_MAX_DIRECT_FINALIZATION_RESULT_BYTES = 64_000_000
_ABSOLUTE_MAX_DIRECT_FINALIZATION_TEXT_BYTES = 8_000_000
_ABSOLUTE_MAX_DIRECT_FINALIZATION_LEGACY_SNAPSHOT_BYTES = 160_000_000
_MAX_PREPARED_PUBLICATION_SETTLEMENT_BYTES = 131_072
_MAX_PREPARED_PUBLICATION_ARCHIVE_BYTES = 20_000_000
_ABSOLUTE_MAX_PREPARED_PUBLICATION_ARCHIVE_BYTES = 160_000_000
_MAX_PUBLICATION_ADMISSION_BYTES = 131_072
_MAX_PUBLICATION_RECOVERY_CERTIFICATE_BYTES = 1_048_576
_MAX_RECEIPT_COLLISION_ROLLBACK_BYTES = 131_072
_MAX_TARGETED_VERIFICATION_INTENT_BYTES = 32_768
_MAX_TARGETED_VERIFICATION_DISPATCH_BYTES = 32_768
_MAX_TARGETED_VERIFICATION_RESULT_BYTES = 262_144
_CONDITIONAL_PUBLICATION_SWAP_INTENT_SCHEMA = (
    "rethlas_conditional_publication_swap_intent_v1"
)
_CONDITIONAL_PUBLICATION_SWAP_CANDIDATE_SCHEMA = (
    "rethlas_conditional_publication_swap_candidate_v1"
)
_CONDITIONAL_PUBLICATION_SWAP_OUTCOME_SCHEMA = (
    "rethlas_conditional_publication_swap_outcome_v1"
)
_MAX_CONDITIONAL_PUBLICATION_SWAP_RECORD_BYTES = 32_768
_RENAME_NOREPLACE = 1
_RENAME_EXCHANGE = 2
_VERIFICATION_CALLER_INSTANCE_ID = "vcaller_" + secrets.token_hex(16)
_FAILED_PHYSICAL_TURN_REQUESTS: set[str] = set()
_ITEM_CONTEXT_ATTESTATION_FIELDS = {
    "item_id",
    "disposition",
    "final_round",
    "expanded_proof_ids",
    "max_chars",
    "context_digest",
    "verdict",
}
_TARGETED_VERIFICATION_LIMIT_FIELDS = {
    "context_max_chars",
    "max_expansion_rounds",
    "max_expanded_proofs",
    "max_expanded_proof_chars",
}


def _publication_lock_timeout_seconds() -> float:
    raw = os.getenv("RETHLAS_PUBLICATION_LOCK_TIMEOUT_SECONDS", "10")
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(
            "RETHLAS_PUBLICATION_LOCK_TIMEOUT_SECONDS must be numeric"
        ) from exc
    if not 0 < value <= 300:
        raise ValueError(
            "RETHLAS_PUBLICATION_LOCK_TIMEOUT_SECONDS must be in (0, 300]"
        )
    return value


def _acquire_publication_lock(handle: Any, *, display_path: Path) -> None:
    deadline = time.monotonic() + _publication_lock_timeout_seconds()
    while True:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError as exc:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    f"timed out waiting for publication lock: {display_path}"
                ) from exc
            time.sleep(min(0.05, remaining))
_TARGETED_RECEIPT_FIELDS = {
    "schema_version",
    "ticket_id",
    "review_id",
    "snapshot_sha256",
    "route_id",
    "blueprint_sha256",
    "blueprint_item_id",
    "blueprint_item_label",
    "claim_sha256",
    "verification_deadline_utc",
    "verification_status",
    "verdict",
    "verification_report",
    "repair_hints",
    "checked_item_ids",
    "context_attestation",
    "verification_limits",
    "proof_context",
    "execution_binding",
    "publication_authority",
    "whole_blueprint_verdict_authority",
    "receipt_sha256",
}
TARGETED_RECEIPT_SCHEMA = "rethlas_targeted_claim_verification_receipt_v4"
MAX_TARGETED_RECEIPT_BYTES = 131_072
PUBLICATION_PROOF_CONTEXT_SCHEMA = "rethlas_publication_proof_context_v3"
_PROOF_CONTEXT_BINDING_FIELDS = {
    "schema_version",
    "source_sha256",
    "proof_item_schema_version",
    "proof_context_schema_version",
    "aggregate_context_schema_version",
    "adaptive_aggregate_context_schema_version",
}
_TARGETED_EXECUTION_BINDING_FIELDS = {
    "schema_version",
    "service_version",
    "closure_sha256",
    "prompt_contract_sha256",
    "backend",
    "prompt_limits",
}
_TARGETED_BACKEND_BINDING_FIELDS = {
    "adapter",
    "provider",
    "model",
    "launch_model",
    "reasoning_effort",
}
_TARGETED_PROMPT_LIMIT_FIELDS = {
    "max_prompt_bytes",
    "max_total_prompt_bytes",
    "max_request_bytes",
    "max_proof_chars",
    "max_statement_chars",
    "max_output_bytes",
    "max_targeted_receipt_bytes",
    "request_timeout_seconds",
    "adapter_timeout_seconds",
    "mcp_tool_timeout_seconds",
}
_TARGETED_PROMPT_LIMIT_V1_FIELDS = _TARGETED_PROMPT_LIMIT_FIELDS - {
    "adapter_timeout_seconds",
    "mcp_tool_timeout_seconds",
}
MAX_PUBLICATION_RECEIPT_BYTES = 16_000_000
MAX_PUBLICATION_PROOF_ITEMS = 20_000
ABSOLUTE_MAX_PUBLICATION_RECEIPT_BYTES = 64_000_000
ABSOLUTE_MAX_PUBLICATION_PROOF_ITEMS = 100_000
ABSOLUTE_VERIFY_CONTEXT_MAX_CHARS = 64_000_000
ABSOLUTE_MAX_EXPANSION_ROUNDS = 4_096
ABSOLUTE_MAX_EXPANDED_PROOFS = 100_000
ABSOLUTE_MAX_EXPANDED_PROOF_CHARS = 64_000_000
ABSOLUTE_MAX_BLUEPRINT_CHARS = 16_000_000
ABSOLUTE_MAX_BLUEPRINT_BYTES = 64_000_000
MAX_BLUEPRINT_CHARS = int(os.getenv("VERIFY_MAX_PROOF_CHARS", "2000000"))
MAX_BLUEPRINT_BYTES = int(os.getenv("VERIFY_MAX_PROOF_BYTES", "8000000"))
if not 0 < MAX_BLUEPRINT_CHARS <= ABSOLUTE_MAX_BLUEPRINT_CHARS:
    raise RuntimeError("VERIFY_MAX_PROOF_CHARS is outside its absolute bound")
if not 0 < MAX_BLUEPRINT_BYTES <= ABSOLUTE_MAX_BLUEPRINT_BYTES:
    raise RuntimeError("VERIFY_MAX_PROOF_BYTES is outside its absolute bound")
_LOOPBACK_HTTP_HOSTS = {"127.0.0.1", "localhost", "::1"}


class VerificationExecutionUnknown(requests.RequestException):
    """The verifier crossed dispatch but lost its trusted process terminal."""


class VerificationSameTurnRetryForbidden(RuntimeError):
    """A failed immutable verifier request cannot replay in one caller process."""


class VerificationOperationalFailure(requests.RequestException):
    """The verifier returned one authenticated, non-mathematical terminal."""

    def __init__(self, detail: Mapping[str, Any]) -> None:
        super().__init__("verification service reported an operational failure")
        self.detail = dict(detail)


class TargetedVerificationExecutionUnknown(VerificationExecutionUnknown):
    """A targeted verifier crossed its durable effect boundary without a result."""

    outcome_state = "execution_unknown"


class TargetedVerificationOperationalBlocked(RuntimeError):
    """A durable targeted-verifier attempt reached a concrete local terminal."""

    outcome_state = "operational_blocked"

    def __init__(self, error_sha256: str, message: str) -> None:
        super().__init__(message)
        self.error_sha256 = error_sha256


class TargetedVerificationPredispatch(TargetedVerificationOperationalBlocked):
    """Recovery found no durable targeted-verifier dispatch."""

    predispatch = True


class TargetedVerificationLocalRetryable(RuntimeError):
    """A recoverable local/transport step left the remote attempt unsettled."""

    local_retryable = True


class _TargetedVerificationDurableRemoteFailure(RuntimeError):
    """An authenticated status response proves one remote failure terminal."""

    def __init__(self, error_sha256: str) -> None:
        super().__init__("targeted verifier returned a durable failure terminal")
        self.error_sha256 = error_sha256


def _raise_for_verification_service_error(response: Any) -> None:
    status_code = getattr(response, "status_code", None)
    if status_code in {502, 503}:
        try:
            payload = response.json()
        except ValueError:
            payload = None
        detail = payload.get("detail") if isinstance(payload, dict) else None
        if (
            isinstance(detail, dict)
            and detail.get("code") == "verifier_execution_unknown"
        ):
            # Deliberately leave ``response`` unset.  The durable caller maps
            # this post-dispatch ambiguity to execution_unknown/no retry.
            raise VerificationExecutionUnknown(
                "verification supervisor terminal was lost after dispatch"
            )
        if (
            status_code == 503
            and isinstance(detail, dict)
            and set(detail)
            == {"code", "adapter", "item_id", "max_output_tokens"}
            and detail.get("code") == "claude_max_output_tokens"
            and detail.get("adapter") == "claude_cli"
            and isinstance(detail.get("item_id"), str)
            and _ITEM_ID_RE.fullmatch(detail["item_id"]) is not None
            and type(detail.get("max_output_tokens")) is int
            and 0 < detail["max_output_tokens"] <= 128_000
        ):
            raise VerificationOperationalFailure(detail)
        if (
            status_code == 503
            and isinstance(detail, dict)
            and set(detail)
            == {"code", "adapter", "item_id", "output_contract"}
            and detail.get("code") == "claude_json_output_invalid"
            and detail.get("adapter") == "claude_cli"
            and isinstance(detail.get("item_id"), str)
            and _ITEM_ID_RE.fullmatch(detail["item_id"]) is not None
            and detail.get("output_contract") == "raw_json_v1"
        ):
            raise VerificationOperationalFailure(detail)
        if (
            status_code == 503
            and isinstance(detail, dict)
            and set(detail)
            == {
                "code",
                "adapter",
                "item_id",
                "structured_output_attempts",
            }
            and detail.get("code")
            == "claude_structured_output_retry_exhausted"
            and detail.get("adapter") == "claude_cli"
            and isinstance(detail.get("item_id"), str)
            and _ITEM_ID_RE.fullmatch(detail["item_id"]) is not None
            and detail.get("structured_output_attempts") == 1
        ):
            raise VerificationOperationalFailure(detail)
    response.raise_for_status()


def _invalid_verifier_response_result(
    *,
    pass_index: int,
    verification_passes: list[dict[str, Any]],
) -> Dict[str, Any]:
    """Return a bounded terminal result once an HTTP response is known invalid."""

    return {
        "published": False,
        "verdict": "wrong",
        "verification_status": "final",
        "publication_blocked_reason": "invalid_verifier_response",
        "invalid_verifier_pass_index": pass_index,
        "verification_passes": list(verification_passes),
        "repair_hints": (
            f"Verifier pass {pass_index} returned an invalid terminal response; "
            "inspect the verifier service before starting a new physical turn."
        ),
    }


def _operational_verifier_failure_result(
    *,
    pass_index: int,
    verification_passes: list[dict[str, Any]],
    failure: VerificationOperationalFailure,
) -> Dict[str, Any]:
    detail = failure.detail
    code = detail["code"]
    result = {
        "published": False,
        "verdict": "wrong",
        "verification_status": "operational_failed",
        "publication_blocked_reason": "operational_verifier_failure",
        "operational_failure_pass_index": pass_index,
        "operational_failure_code": code,
        "operational_failure_item_id": detail["item_id"],
        "verification_passes": list(verification_passes),
    }
    if code == "claude_max_output_tokens":
        result["operational_failure_output_token_limit"] = detail[
            "max_output_tokens"
        ]
        reason = "The verifier hit its effective provider output-token ceiling."
    elif code == "claude_structured_output_retry_exhausted":
        result["operational_failure_structured_output_attempts"] = detail[
            "structured_output_attempts"
        ]
        reason = (
            "The verifier's single structured-output attempt did not satisfy "
            "the response schema."
        )
    else:
        result["operational_failure_output_contract"] = detail[
            "output_contract"
        ]
        reason = (
            "The verifier completed, but its raw final response was not one "
            "valid schema-conforming JSON object."
        )
    result["repair_hints"] = (
        reason
        + " This is an operational failure, not a mathematical verdict; "
        "resume the same immutable attempt only in a later explicit "
        "physical turn."
    )
    return result


def _nonindependent_verifier_quorum_result(
    *, verification_passes: list[dict[str, Any]]
) -> Dict[str, Any]:
    """Return a durable terminal for two concrete but nonindependent passes."""

    return {
        "published": False,
        "verdict": "wrong",
        "verification_status": "final",
        "publication_blocked_reason": "verifier_quorum_not_independent",
        "verification_passes": list(verification_passes),
        "repair_hints": (
            "The verifier service reused an attempt or run identity across "
            "the required independent passes; inspect the service before a "
            "new physical turn."
        ),
    }


def _publication_target_collision_result(
    *,
    verification_passes: list[dict[str, Any]],
) -> Dict[str, Any]:
    """Return a definite terminal when another local proof won publication."""

    return {
        "published": False,
        "verdict": "wrong",
        "verification_status": "final",
        "publication_blocked_reason": "verified_target_collision",
        "verification_passes": list(verification_passes),
        "repair_hints": (
            "A different verified blueprint occupies the publication target; "
            "preserve it and start a new explicitly authorized publication turn."
        ),
    }


def _nonnegative_limit(name: str, default: int, *, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if value < 0:
        raise RuntimeError(f"{name} must be nonnegative")
    if value > maximum:
        raise RuntimeError(f"{name} exceeds the protocol absolute maximum")
    return value


VERIFY_CONTEXT_MAX_CHARS = _nonnegative_limit(
    "VERIFY_CONTEXT_MAX_CHARS",
    200_000,
    maximum=ABSOLUTE_VERIFY_CONTEXT_MAX_CHARS,
)
MAX_EXPANSION_ROUNDS = _nonnegative_limit(
    "VERIFY_MAX_EXPANSION_ROUNDS",
    2,
    maximum=ABSOLUTE_MAX_EXPANSION_ROUNDS,
)
MAX_EXPANDED_PROOFS = _nonnegative_limit(
    "VERIFY_MAX_EXPANDED_PROOFS",
    8,
    maximum=ABSOLUTE_MAX_EXPANDED_PROOFS,
)
MAX_EXPANDED_PROOF_CHARS = _nonnegative_limit(
    "VERIFY_MAX_EXPANDED_PROOF_CHARS",
    200_000,
    maximum=ABSOLUTE_MAX_EXPANDED_PROOF_CHARS,
)


def _publication_proof_context_sha256() -> str:
    source_path = Path(__file__).resolve(strict=True).with_name(
        "publication_proof_context_v3.py"
    )
    return hashlib.sha256(source_path.read_bytes()).hexdigest()


_PINNED_PUBLICATION_PROOF_CONTEXT_SHA256 = _publication_proof_context_sha256()


def _verification_client_source_sha256() -> str:
    return hashlib.sha256(Path(__file__).resolve(strict=True).read_bytes()).hexdigest()


_PINNED_VERIFICATION_CLIENT_SOURCE_SHA256 = _verification_client_source_sha256()


def _assert_publication_proof_context_unchanged() -> str:
    current = _publication_proof_context_sha256()
    if current != _PINNED_PUBLICATION_PROOF_CONTEXT_SHA256:
        raise RuntimeError("publication proof-context source changed after import")
    return _PINNED_PUBLICATION_PROOF_CONTEXT_SHA256


def _current_proof_context_binding() -> dict[str, Any]:
    return {
        "schema_version": PUBLICATION_PROOF_CONTEXT_SCHEMA,
        "source_sha256": _assert_publication_proof_context_unchanged(),
        "proof_item_schema_version": PROOF_ITEM_SCHEMA_VERSION,
        "proof_context_schema_version": PROOF_CONTEXT_SCHEMA_VERSION,
        "aggregate_context_schema_version": AGGREGATE_CONTEXT_SCHEMA_VERSION,
        "adaptive_aggregate_context_schema_version": (
            ADAPTIVE_AGGREGATE_CONTEXT_SCHEMA_VERSION
        ),
    }


def _validate_proof_context_binding(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != _PROOF_CONTEXT_BINDING_FIELDS:
        raise ValueError("targeted proof-context binding has an invalid shape")
    binding = dict(value)
    if (
        binding.get("schema_version") != PUBLICATION_PROOF_CONTEXT_SCHEMA
        or not isinstance(binding.get("source_sha256"), str)
        or _HEX_DIGEST_RE.fullmatch(binding["source_sha256"]) is None
        or any(
            isinstance(binding.get(name), bool)
            or not isinstance(binding.get(name), int)
            or binding[name] <= 0
            for name in (
                "proof_item_schema_version",
                "proof_context_schema_version",
                "aggregate_context_schema_version",
                "adaptive_aggregate_context_schema_version",
            )
        )
    ):
        raise ValueError("targeted proof-context binding is invalid")
    return binding


def _assert_verification_client_source_unchanged() -> str:
    current = _verification_client_source_sha256()
    if current != _PINNED_VERIFICATION_CLIENT_SOURCE_SHA256:
        raise RuntimeError("verification client source changed after import")
    return current


def _absolute_path(path: Path) -> Path:
    """Return a normalized absolute path without resolving any symlinks."""

    return Path(os.path.abspath(os.fspath(path)))


def _validate_endpoint(endpoint: str) -> str:
    if (
        not isinstance(endpoint, str)
        or not endpoint
        or len(endpoint.encode("utf-8")) > 8192
    ):
        raise ValueError("verification endpoint must be a non-empty URL")
    try:
        parsed = urlsplit(endpoint)
        hostname = parsed.hostname
        # Accessing port also rejects malformed and out-of-range values.
        parsed.port
    except ValueError as exc:
        raise ValueError("verification endpoint is not a valid URL") from exc
    if hostname is None or parsed.username is not None or parsed.password is not None:
        raise ValueError(
            "verification endpoint must have a host and must not contain userinfo"
        )
    if parsed.query or parsed.fragment:
        raise ValueError(
            "verification endpoint must not contain a query or fragment"
        )
    if parsed.scheme == "https":
        return endpoint
    if parsed.scheme == "http" and hostname.lower() in _LOOPBACK_HTTP_HOSTS:
        return endpoint
    raise ValueError(
        "verification endpoint must use HTTPS or HTTP on "
        "127.0.0.1, localhost, or ::1"
    )


def _profile_endpoint(endpoint: str) -> str:
    parsed = urlsplit(endpoint)
    path = parsed.path
    if path.endswith("/verify"):
        path = path[: -len("/verify")] + "/profile"
    else:
        path = "/profile"
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def _targeted_status_endpoint(endpoint: str, targeted_attempt_id: str) -> str:
    if re.fullmatch(r"target_[0-9a-f]{32}", targeted_attempt_id) is None:
        raise ValueError("targeted attempt id is invalid")
    parsed = urlsplit(endpoint)
    path = parsed.path.rstrip("/") + "/status/" + targeted_attempt_id
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def _whole_pass_status_endpoint(endpoint: str, verification_attempt_id: str) -> str:
    if _VERIFICATION_ATTEMPT_RE.fullmatch(verification_attempt_id) is None:
        raise ValueError("verification attempt id is invalid")
    parsed = urlsplit(endpoint)
    path = parsed.path.rstrip("/") + "/status/" + verification_attempt_id
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def _endpoint_uses_local_lifeline(endpoint: str) -> bool:
    """Return whether verifier and caller share the local PID namespace."""

    hostname = urlsplit(endpoint).hostname
    return hostname is not None and hostname.lower() in _LOOPBACK_HTTP_HOSTS


def _validate_verifier_profile(
    value: object, *, expected_profile: str
) -> list[dict[str, Any]]:
    if expected_profile not in {
        "compatible",
        "balanced",
        "economy",
        "max_diversity",
    }:
        raise ValueError("verification profile is unsupported")
    expected_keys = {
        "schema_version",
        "service_version",
        "profile",
        "passes",
        "automatic_tiebreaker",
        "fallback_policy",
    }
    if (
        not isinstance(value, dict)
        or set(value) != expected_keys
        or value["schema_version"] != "rethlas_verifier_profile_v1"
        or not isinstance(value["service_version"], str)
        or not value["service_version"]
        or value["profile"] != expected_profile
        or value["automatic_tiebreaker"] is not False
        or value["fallback_policy"] != "forbid"
        or not isinstance(value["passes"], list)
        or len(value["passes"]) != 2
    ):
        raise ValueError("verification service profile binding mismatch")
    passes: list[dict[str, Any]] = []
    for index, item in enumerate(value["passes"], 1):
        if (
            not isinstance(item, dict)
            or set(item)
            != {
                "pass_index",
                "adapter",
                "provider",
                "model",
                "launch_model",
                "reasoning_effort",
                "session_mode",
            }
            or isinstance(item["pass_index"], bool)
            or item["pass_index"] != index
            or item["adapter"] not in {"codex_cli", "claude_cli"}
            or not isinstance(item["provider"], str)
            or not item["provider"]
            or not isinstance(item["model"], str)
            or not item["model"]
            or not isinstance(item["launch_model"], str)
            or not item["launch_model"]
            or item["reasoning_effort"]
            not in {"low", "medium", "high", "xhigh", "max"}
            or item["session_mode"] != "cold"
        ):
            raise ValueError("verification service pass profile is invalid")
        passes.append(dict(item))
    if expected_profile in {"balanced", "economy", "max_diversity"}:
        if passes[0]["model"] == passes[1]["model"]:
            raise ValueError("selected verification profile requires distinct models")
    if expected_profile == "max_diversity" and not (
        passes[0]["adapter"] == "codex_cli"
        and passes[0]["provider"] == "openai"
        and passes[1]["adapter"] == "claude_cli"
        and passes[1]["provider"] != "openai"
        and passes[0]["provider"] != passes[1]["provider"]
    ):
        raise ValueError("max_diversity verifier adapters are not diverse")
    return passes


def _process_start_identity(pid: int) -> str | None:
    if type(pid) is not int or pid <= 1:
        return None
    if sys.platform.startswith("linux"):
        try:
            raw = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
            tail = raw[raw.rindex(")") + 2 :].split()
            if tail[0] == "Z":
                return None
            start_ticks = tail[19]
        except (OSError, UnicodeError, ValueError, IndexError):
            return None
        return f"linux:{start_ticks}"
    if sys.platform == "darwin":
        class ProcBsdInfo(ctypes.Structure):
            _fields_ = [
                ("pbi_flags", ctypes.c_uint32),
                ("pbi_status", ctypes.c_uint32),
                ("pbi_xstatus", ctypes.c_uint32),
                ("pbi_pid", ctypes.c_uint32),
                ("pbi_ppid", ctypes.c_uint32),
                ("pbi_uid", ctypes.c_uint32),
                ("pbi_gid", ctypes.c_uint32),
                ("pbi_ruid", ctypes.c_uint32),
                ("pbi_rgid", ctypes.c_uint32),
                ("pbi_svuid", ctypes.c_uint32),
                ("pbi_svgid", ctypes.c_uint32),
                ("pbi_rfu_1", ctypes.c_uint32),
                ("pbi_comm", ctypes.c_char * 16),
                ("pbi_name", ctypes.c_char * 32),
                ("pbi_nfiles", ctypes.c_uint32),
                ("pbi_pgid", ctypes.c_uint32),
                ("pbi_pjobc", ctypes.c_uint32),
                ("e_tdev", ctypes.c_uint32),
                ("e_tpgid", ctypes.c_uint32),
                ("pbi_nice", ctypes.c_int32),
                ("pbi_start_tvsec", ctypes.c_uint64),
                ("pbi_start_tvusec", ctypes.c_uint64),
            ]

        try:
            libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
            proc_pidinfo = libproc.proc_pidinfo
            proc_pidinfo.argtypes = [
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_uint64,
                ctypes.c_void_p,
                ctypes.c_int,
            ]
            proc_pidinfo.restype = ctypes.c_int
            info = ProcBsdInfo()
            copied = proc_pidinfo(
                pid,
                3,
                0,
                ctypes.byref(info),
                ctypes.sizeof(info),
            )
        except (AttributeError, OSError):
            return None
        if copied != ctypes.sizeof(info) or info.pbi_pid != pid or info.pbi_status == 5:
            return None
        return f"darwin:{info.pbi_start_tvsec}:{info.pbi_start_tvusec}"
    return None


def _verification_caller_binding() -> tuple[str, int, str]:
    pid = os.getpid()
    identity = _process_start_identity(pid)
    if identity is None:
        raise RuntimeError("cannot bind the verifier caller process identity")
    return (
        _VERIFICATION_CALLER_INSTANCE_ID,
        pid,
        hashlib.sha256(identity.encode("ascii")).hexdigest(),
    )


def _verification_pass_identity(
    *,
    statement: str,
    proof_digest_value: str,
    context_digest: str,
    checked_item_ids: list[str],
    verifier_profile: str,
    verifier_service_version: str,
    verifier_pass: Mapping[str, Any],
    pass_index: int,
) -> tuple[str, str]:
    role = "primary" if pass_index == 1 else "adversarial_full_claim_audit"
    identity = {
        "schema_version": _VERIFIER_PASS_IDENTITY_SCHEMA,
        "statement_target_digest": proof_digest(
            extract_verification_target(statement)
        ),
        "proof_digest": proof_digest_value,
        "context_digest": context_digest,
        "checked_item_ids": checked_item_ids,
        "verifier_profile": verifier_profile,
        "verifier_adapter": verifier_pass["adapter"],
        "verifier_provider": verifier_pass["provider"],
        "verifier_model": verifier_pass["model"],
        "verifier_launch_model": verifier_pass["launch_model"],
        "verifier_reasoning_effort": verifier_pass["reasoning_effort"],
        "verifier_service_version": verifier_service_version,
        "verification_pass_index": pass_index,
        "verification_role": role,
    }
    digest = hashlib.sha256(
        json.dumps(
            identity,
            allow_nan=False,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return digest, "veratt_" + digest[:32]


def _publication_verifier_effect_binding(
    *,
    endpoint: str,
    timeout_seconds: float,
    api_token: str | None,
    statement: str,
    proof_digest_value: str,
    context_digest: str,
    checked_item_ids: list[str],
    verification_profile: str,
) -> dict[str, Any]:
    profile_kwargs: Dict[str, Any] = {
        "timeout": min(5.0, timeout_seconds),
    }
    if api_token:
        profile_kwargs["headers"] = {
            "Authorization": f"Bearer {api_token}"
        }
    profile_response = requests.get(
        _profile_endpoint(endpoint), **profile_kwargs
    )
    profile_response.raise_for_status()
    try:
        raw_profile = profile_response.json()
    except (ValueError, RecursionError) as exc:
        raise ValueError(
            "verification service profile returned non-JSON"
        ) from exc
    expected_passes = _validate_verifier_profile(
        raw_profile, expected_profile=verification_profile
    )
    assert isinstance(raw_profile, dict)
    verifier_service_version = raw_profile["service_version"]
    pass_bindings: list[tuple[Mapping[str, Any], str, str]] = []
    for pass_index, expected_pass in enumerate(expected_passes, start=1):
        pass_identity, attempt_id = _verification_pass_identity(
            statement=statement,
            proof_digest_value=proof_digest_value,
            context_digest=context_digest,
            checked_item_ids=checked_item_ids,
            verifier_profile=verification_profile,
            verifier_service_version=verifier_service_version,
            verifier_pass=expected_pass,
            pass_index=pass_index,
        )
        pass_bindings.append((expected_pass, pass_identity, attempt_id))
    effect_preimage = {
        "schema_version": "rethlas_publication_verifier_effect_identity_v1",
        "verifier_service_version": verifier_service_version,
        "verification_profile": verification_profile,
        "passes": [
            {
                "pass_index": pass_index,
                "verification_pass_identity": binding[1],
                "verification_attempt_id": binding[2],
            }
            for pass_index, binding in enumerate(pass_bindings, start=1)
        ],
    }
    return {
        "raw_profile": raw_profile,
        "expected_passes": expected_passes,
        "verifier_service_version": verifier_service_version,
        "pass_bindings": pass_bindings,
        "effect_preimage": effect_preimage,
        "effect_identity_sha256": hashlib.sha256(
            _canonical_json_line_bytes(effect_preimage)
        ).hexdigest(),
    }


def _read_restartable_whole_pass_status(
    *,
    endpoint: str,
    timeout_seconds: float,
    api_token: str | None,
    verification_attempt_id: str,
    verification_pass_identity: str,
) -> dict[str, Any]:
    request_kwargs: Dict[str, Any] = {
        "params": {
            "verification_pass_identity": verification_pass_identity,
        },
        "timeout": min(30.0, timeout_seconds),
    }
    if api_token:
        request_kwargs["headers"] = {
            "Authorization": f"Bearer {api_token}"
        }
    response = requests.get(
        _whole_pass_status_endpoint(endpoint, verification_attempt_id),
        **request_kwargs,
    )
    try:
        payload = response.json()
    except (ValueError, RecursionError) as exc:
        raise VerificationExecutionUnknown(
            "whole verifier status returned non-JSON"
        ) from exc
    detail = payload.get("detail") if isinstance(payload, dict) else None
    if response.status_code != 409 or not isinstance(detail, dict):
        raise VerificationExecutionUnknown(
            "whole verifier status is not a restartable operational checkpoint"
        )
    snapshot_sha256 = detail.get("snapshot_sha256")
    seed = dict(detail)
    seed.pop("snapshot_sha256", None)
    expected_snapshot_sha256 = hashlib.sha256(
        json.dumps(
            seed,
            allow_nan=False,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    expected_keys = {
        "schema_version",
        "verification_attempt_id",
        "pass_identity_sha256",
        "state",
        "intent_sha256",
        "caller_instance_id",
        "retry_ordinal",
        "current_item_id",
        "current_item_index",
        "failure_status_code",
        "failure_sha256",
        "aggregate_sha256",
        "resumable_by_this_service",
        "publication_aggregate_present",
        "snapshot_sha256",
    }
    if (
        set(detail) != expected_keys
        or detail.get("schema_version")
        != "rethlas_verifier_pass_status_snapshot_v1"
        or detail.get("verification_attempt_id") != verification_attempt_id
        or detail.get("pass_identity_sha256")
        != verification_pass_identity
        or detail.get("state") != "operational_failed"
        or detail.get("resumable_by_this_service") is not True
        or detail.get("publication_aggregate_present") is not False
        or detail.get("aggregate_sha256") is not None
        or not isinstance(detail.get("caller_instance_id"), str)
        or _VERIFICATION_CALLER_RE.fullmatch(detail["caller_instance_id"])
        is None
        or detail["caller_instance_id"] == _VERIFICATION_CALLER_INSTANCE_ID
        or not isinstance(detail.get("intent_sha256"), str)
        or _HEX_DIGEST_RE.fullmatch(detail["intent_sha256"]) is None
        or not isinstance(detail.get("failure_sha256"), str)
        or _HEX_DIGEST_RE.fullmatch(detail["failure_sha256"]) is None
        or type(detail.get("failure_status_code")) is not int
        or not 400 <= detail["failure_status_code"] <= 599
        or snapshot_sha256 != expected_snapshot_sha256
    ):
        raise VerificationExecutionUnknown(
            "whole verifier operational checkpoint binding mismatch"
        )
    return dict(detail)


def _open_directory(path: Path, *, label: str) -> int:
    """Open a directory without following its final path component."""

    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ValueError(f"{label} must be an existing non-symlink directory: {path}") from exc
    if not stat.S_ISDIR(os.fstat(descriptor).st_mode):  # pragma: no cover - O_DIRECTORY
        os.close(descriptor)
        raise ValueError(f"{label} must be a directory: {path}")
    return descriptor


def _open_or_create_directory_durable(path: Path, *, label: str) -> int:
    """Create an absolute directory chain and durably publish every new edge."""

    path = _absolute_path(path)
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path.anchor, flags)
    try:
        for part in path.parts[1:]:
            while True:
                try:
                    next_descriptor = os.open(part, flags, dir_fd=descriptor)
                    break
                except FileNotFoundError:
                    try:
                        os.mkdir(part, 0o700, dir_fd=descriptor)
                    except FileExistsError:
                        continue
                    next_descriptor = os.open(
                        part, flags, dir_fd=descriptor
                    )
                    break
                except OSError as exc:
                    raise ValueError(
                        f"{label} must use non-symlink directories: {path}"
                    ) from exc
            if not stat.S_ISDIR(os.fstat(next_descriptor).st_mode):
                os.close(next_descriptor)
                raise ValueError(f"{label} must be a directory: {path}")
            # The edge may have been created by a shell `mkdir -p` or a racing
            # process that did not sync it.  Sync every traversed parent, not
            # only edges this call happened to create, before any child record
            # can become the durable dispatch fence.
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _assert_directory_binding(path: Path, descriptor: int, *, label: str) -> None:
    """Require *path* to still name the directory held by *descriptor*."""

    held = os.fstat(descriptor)
    try:
        current_descriptor = _open_directory(path, label=label)
    except ValueError as exc:
        raise ValueError(f"{label} changed during verification: {path}") from exc
    try:
        current = os.fstat(current_descriptor)
        if (held.st_dev, held.st_ino) != (current.st_dev, current.st_ino):
            raise ValueError(f"{label} changed during verification: {path}")
    finally:
        os.close(current_descriptor)


def _directory_parts_beneath(
    root: Path,
    target: Path,
    *,
    label: str,
) -> tuple[str, ...]:
    """Return lexical target components below *root* without resolving links."""

    try:
        relative = target.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"{label} must be inside trusted blueprint root: {root}") from exc
    parts = relative.parts
    if any(part in {"", ".", ".."} or "/" in part for part in parts):
        raise ValueError(f"{label} has unsafe path components: {target}")
    return parts


def _open_directory_at(
    root_fd: int,
    parts: tuple[str, ...],
    *,
    label: str,
) -> int:
    """Walk directory components relative to a held root without following links."""

    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.dup(root_fd)
    try:
        for part in parts:
            next_descriptor = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):  # pragma: no cover
            raise ValueError(f"{label} must be a directory")
        return descriptor
    except Exception as exc:
        os.close(descriptor)
        if isinstance(exc, ValueError):
            raise
        raise ValueError(
            f"{label} must be reachable through non-symlink directories"
        ) from exc


def _assert_directory_at_binding(
    root_fd: int,
    parts: tuple[str, ...],
    descriptor: int,
    *,
    label: str,
) -> None:
    """Require the root-relative path to still name the held directory."""

    held = os.fstat(descriptor)
    try:
        current_descriptor = _open_directory_at(root_fd, parts, label=label)
    except ValueError as exc:
        raise ValueError(f"{label} changed during verification") from exc
    try:
        current = os.fstat(current_descriptor)
        if (held.st_dev, held.st_ino) != (current.st_dev, current.st_ino):
            raise ValueError(f"{label} changed during verification")
    finally:
        os.close(current_descriptor)


def _read_regular_blueprint_at(
    directory_fd: int,
    filename: str,
    *,
    display_path: Path,
    label: str,
    maximum_bytes: int | None = None,
    maximum_chars: int | None = None,
) -> str:
    """Read a bounded regular file relative to an already trusted directory."""

    if maximum_bytes is None:
        maximum_bytes = MAX_BLUEPRINT_BYTES
    if maximum_chars is None:
        maximum_chars = MAX_BLUEPRINT_CHARS
    if (
        isinstance(maximum_bytes, bool)
        or not isinstance(maximum_bytes, int)
        or not 0 < maximum_bytes <= ABSOLUTE_MAX_BLUEPRINT_BYTES
        or isinstance(maximum_chars, bool)
        or not isinstance(maximum_chars, int)
        or not 0 < maximum_chars <= ABSOLUTE_MAX_BLUEPRINT_CHARS
    ):
        raise ValueError(f"{label} read cap is invalid")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(filename, flags, dir_fd=directory_fd)
    except OSError as exc:
        raise ValueError(
            f"{label} must be an existing regular file: {display_path}"
        ) from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError(f"{label} must be a regular file: {display_path}")
        if metadata.st_size > maximum_bytes:
            raise ValueError(f"{label} exceeds VERIFY_MAX_PROOF_BYTES")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            raw = handle.read(maximum_bytes + 1)
        if len(raw) > maximum_bytes:
            raise ValueError(f"{label} exceeds VERIFY_MAX_PROOF_BYTES")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"{label} must be valid UTF-8") from exc
        if len(text) > maximum_chars:
            raise ValueError(f"{label} exceeds VERIFY_MAX_PROOF_CHARS")
        return text
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _open_lock_file_at(
    directory_fd: int,
    filename: str,
    *,
    display_path: Path,
) -> Any:
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    while True:
        try:
            descriptor = os.open(
                filename,
                os.O_RDWR | nofollow,
                dir_fd=directory_fd,
            )
            break
        except FileNotFoundError:
            try:
                descriptor = os.open(
                    filename,
                    os.O_RDWR | os.O_CREAT | os.O_EXCL | nofollow,
                    0o600,
                    dir_fd=directory_fd,
                )
                break
            except FileExistsError:
                # Another publisher won the lock-file creation race. Open the
                # now-existing inode without O_CREAT on the next pass.
                continue
            except OSError as exc:
                raise ValueError(
                    f"publication lock must not be a symlink: {display_path}"
                ) from exc
        except OSError as exc:
            raise ValueError(
                f"publication lock must not be a symlink: {display_path}"
            ) from exc
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError(
                f"publication lock must be a regular file: {display_path}"
            )
        return os.fdopen(descriptor, "r+", encoding="utf-8")
    except Exception:
        os.close(descriptor)
        raise


def _lstat_at(directory_fd: int, filename: str) -> os.stat_result | None:
    try:
        return os.stat(filename, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None


_PUBLICATION_TARGET_PRECONDITION_FIELDS = {
    "kind", "st_dev", "st_ino", "st_size", "st_mtime_ns", "content_sha256",
}


def _publication_target_precondition_at(
    directory_fd: int,
    filename: str,
    *,
    display_path: Path,
    maximum_bytes: int | None = None,
) -> dict[str, Any]:
    """Capture the exact replaceable target state while its lock is held."""

    if maximum_bytes is None:
        maximum_bytes = MAX_BLUEPRINT_BYTES
    if (
        isinstance(maximum_bytes, bool)
        or not isinstance(maximum_bytes, int)
        or not 0 < maximum_bytes <= ABSOLUTE_MAX_BLUEPRINT_BYTES
    ):
        raise ValueError("verified blueprint target read cap is invalid")
    metadata = _lstat_at(directory_fd, filename)
    if metadata is None:
        return {
            "kind": "absent",
            "st_dev": None,
            "st_ino": None,
            "st_size": None,
            "st_mtime_ns": None,
            "content_sha256": None,
        }
    if stat.S_ISREG(metadata.st_mode):
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(filename, flags, dir_fd=directory_fd)
        except OSError as exc:
            raise ValueError(
                f"verified blueprint target changed while read: {display_path}"
            ) from exc
        try:
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or (opened.st_dev, opened.st_ino)
                != (metadata.st_dev, metadata.st_ino)
                or (opened.st_size, opened.st_mtime_ns)
                != (metadata.st_size, metadata.st_mtime_ns)
                or opened.st_size > maximum_bytes
            ):
                raise ValueError(
                    "verified blueprint target is unsafe or exceeds its cap"
                )
            with os.fdopen(descriptor, "rb") as handle:
                descriptor = -1
                raw = handle.read(maximum_bytes + 1)
                after = os.fstat(handle.fileno())
                if (
                    after.st_size,
                    after.st_mtime_ns,
                    after.st_ctime_ns,
                ) != (
                    opened.st_size,
                    opened.st_mtime_ns,
                    opened.st_ctime_ns,
                ):
                    raise ValueError(
                        "verified blueprint target changed while read"
                    )
            if len(raw) > maximum_bytes:
                raise ValueError("verified blueprint target exceeds its byte cap")
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        digest = hashlib.sha256(raw).hexdigest()
        kind = "regular"
    elif stat.S_ISLNK(metadata.st_mode):
        try:
            target = os.readlink(filename, dir_fd=directory_fd)
        except OSError as exc:
            raise ValueError(
                f"verified blueprint symlink changed while read: {display_path}"
            ) from exc
        digest = hashlib.sha256(os.fsencode(target)).hexdigest()
        kind = "symlink"
    else:
        raise ValueError(
            "verified blueprint target must be absent, regular, or a symlink"
        )
    return {
        "kind": kind,
        "st_dev": metadata.st_dev,
        "st_ino": metadata.st_ino,
        "st_size": metadata.st_size,
        "st_mtime_ns": metadata.st_mtime_ns,
        "content_sha256": digest,
    }


def _validate_publication_target_precondition(
    value: object,
) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != (
        _PUBLICATION_TARGET_PRECONDITION_FIELDS
    ):
        raise ValueError("publication target precondition shape mismatch")
    kind = value.get("kind")
    bindings = (
        value.get("st_dev"),
        value.get("st_ino"),
        value.get("st_size"),
        value.get("st_mtime_ns"),
        value.get("content_sha256"),
    )
    if kind == "absent":
        if bindings != (None, None, None, None, None):
            raise ValueError("absent publication target has inode bindings")
    elif kind in {"regular", "symlink"}:
        for field in bindings[:4]:
            if isinstance(field, bool) or not isinstance(field, int) or field < 0:
                raise ValueError("publication target inode binding mismatch")
        if (
            not isinstance(bindings[4], str)
            or _HEX_DIGEST_RE.fullmatch(bindings[4]) is None
        ):
            raise ValueError("publication target content binding mismatch")
    else:
        raise ValueError("publication target kind is invalid")
    return dict(value)


def _write_all(descriptor: int, content: bytes) -> None:
    view = memoryview(content)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:  # pragma: no cover - os.write either writes or raises
            raise OSError("short write while publishing verified blueprint")
        view = view[written:]


def _atomic_replace_at(
    directory_fd: int,
    filename: str,
    content: bytes,
) -> tuple[int, int]:
    """Atomically replace *filename* using only operations relative to *directory_fd*."""

    temporary_name = f".{filename}.{secrets.token_hex(16)}.tmp"
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(temporary_name, flags, 0o600, dir_fd=directory_fd)
        try:
            _write_all(descriptor, content)
            os.fsync(descriptor)
            os.replace(
                temporary_name,
                filename,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
            )
            published = os.fstat(descriptor)
            os.fsync(directory_fd)
            return published.st_dev, published.st_ino
        finally:
            os.close(descriptor)
    finally:
        try:
            os.unlink(temporary_name, dir_fd=directory_fd)
        except FileNotFoundError:
            pass


def _renameat2_at(
    directory_fd: int, source: str, destination: str, flags: int
) -> None:
    """Invoke Linux renameat2 without falling back to an unsafe blind rename."""

    try:
        renameat2 = ctypes.CDLL(None, use_errno=True).renameat2
    except AttributeError as exc:  # pragma: no cover - supported hosts are Linux
        raise OSError(errno.ENOSYS, "renameat2 is unavailable") from exc
    renameat2.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    renameat2.restype = ctypes.c_int
    result = renameat2(
        directory_fd,
        os.fsencode(source),
        directory_fd,
        os.fsencode(destination),
        flags,
    )
    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), destination)


def _read_canonical_record_at(
    directory_fd: int,
    filename: str,
    *,
    maximum_bytes: int,
    label: str,
) -> dict[str, Any] | None:
    descriptor = -1
    try:
        try:
            descriptor = os.open(
                filename,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=directory_fd,
            )
        except FileNotFoundError:
            return None
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > maximum_bytes:
            raise ValueError(f"{label} is unsafe or exceeds its byte cap")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            raw = handle.read(maximum_bytes + 1)
        if len(raw) > maximum_bytes:
            raise ValueError(f"{label} exceeds its byte cap")
        try:
            value = json.loads(raw.decode("utf-8"))
            canonical = _canonical_json_line_bytes(value)
        except (UnicodeError, TypeError, ValueError, RecursionError) as exc:
            raise ValueError(f"{label} is not canonical JSON") from exc
        if not isinstance(value, dict) or raw != canonical:
            raise ValueError(f"{label} is not canonical JSON")
        return value
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _write_once_canonical_record_at(
    directory_fd: int,
    filename: str,
    value: dict[str, Any],
    *,
    maximum_bytes: int,
    label: str,
) -> dict[str, Any]:
    encoded = _canonical_json_line_bytes(value)
    if len(encoded) > maximum_bytes:
        raise ValueError(f"{label} exceeds its byte cap")
    existing = _read_canonical_record_at(
        directory_fd,
        filename,
        maximum_bytes=maximum_bytes,
        label=label,
    )
    if existing is not None:
        if existing != value:
            raise ValueError(f"{label} changed on replay")
        return existing
    temporary = f".{filename}.{secrets.token_hex(16)}.tmp"
    descriptor = -1
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=directory_fd,
        )
        _write_all(descriptor, encoded)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        try:
            os.link(
                temporary,
                filename,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
                follow_symlinks=False,
            )
        except FileExistsError:
            pass
        else:
            os.fsync(directory_fd)
        observed = _read_canonical_record_at(
            directory_fd,
            filename,
            maximum_bytes=maximum_bytes,
            label=label,
        )
        if observed != value:
            raise ValueError(f"{label} collided before publication")
        return observed
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=directory_fd)
        except FileNotFoundError:
            pass


def _regular_file_identity_at(
    directory_fd: int, filename: str
) -> tuple[int, int] | None:
    metadata = _lstat_at(directory_fd, filename)
    if metadata is None or not stat.S_ISREG(metadata.st_mode):
        return None
    return metadata.st_dev, metadata.st_ino


def _regular_content_matches_at(
    directory_fd: int,
    filename: str,
    *,
    content: bytes,
    identity: tuple[int, int] | None = None,
) -> tuple[int, int] | None:
    metadata = _lstat_at(directory_fd, filename)
    if (
        metadata is None
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_size != len(content)
        or (
            identity is not None
            and (metadata.st_dev, metadata.st_ino) != identity
        )
    ):
        return None
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(filename, flags, dir_fd=directory_fd)
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (opened.st_dev, opened.st_ino)
            != (metadata.st_dev, metadata.st_ino)
        ):
            return None
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            raw = handle.read(len(content) + 1)
            after = os.fstat(handle.fileno())
        if (
            (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            != (
                opened.st_dev,
                opened.st_ino,
                opened.st_size,
                opened.st_mtime_ns,
            )
            or raw != content
        ):
            return None
        return opened.st_dev, opened.st_ino
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _conditional_publication_swap_paths(
    filename: str,
    expected_precondition: Mapping[str, Any],
    content: bytes,
) -> tuple[str, str, str, str, str]:
    seed = {
        "schema_version": "rethlas_conditional_publication_swap_identity_v1",
        "filename": filename,
        "expected_target_precondition": dict(expected_precondition),
        "candidate_sha256": hashlib.sha256(content).hexdigest(),
        "candidate_bytes": len(content),
    }
    key = hashlib.sha256(_canonical_json_line_bytes(seed)).hexdigest()
    prefix = f".rethlas-target-swap-{key}"
    return (
        key,
        prefix + ".intent.json",
        prefix + ".candidate.json",
        prefix + ".outcome.json",
        prefix + ".candidate.tmp",
    )


def _conditional_replace_at(
    directory_fd: int,
    filename: str,
    content: bytes,
    *,
    expected_precondition: Mapping[str, Any],
    display_path: Path,
    retain_displaced: bool = False,
    maximum_target_bytes: int | None = None,
) -> tuple[int, int] | None:
    """Recoverably replace a target iff its pinned precondition still holds."""

    expected = _validate_publication_target_precondition(
        dict(expected_precondition)
    )
    if maximum_target_bytes is None:
        maximum_target_bytes = MAX_BLUEPRINT_BYTES
    (
        operation_key,
        intent_name,
        candidate_record_name,
        outcome_name,
        candidate_name,
    ) = _conditional_publication_swap_paths(filename, expected, content)
    intent = _read_canonical_record_at(
        directory_fd,
        intent_name,
        maximum_bytes=_MAX_CONDITIONAL_PUBLICATION_SWAP_RECORD_BYTES,
        label="conditional publication swap intent",
    )
    if intent is None:
        intent = {
            "schema_version": _CONDITIONAL_PUBLICATION_SWAP_INTENT_SCHEMA,
            "status": "prepared",
            "operation_key": operation_key,
            "filename": filename,
            "candidate_name": candidate_name,
            "candidate_sha256": hashlib.sha256(content).hexdigest(),
            "candidate_bytes": len(content),
            "expected_target_precondition": expected,
            "prepared_at_utc": datetime.now(timezone.utc).isoformat(),
        }
        intent = _write_once_canonical_record_at(
            directory_fd,
            intent_name,
            intent,
            maximum_bytes=_MAX_CONDITIONAL_PUBLICATION_SWAP_RECORD_BYTES,
            label="conditional publication swap intent",
        )
    if (
        set(intent)
        != {
            "schema_version", "status", "operation_key", "filename",
            "candidate_name", "candidate_sha256", "candidate_bytes",
            "expected_target_precondition", "prepared_at_utc",
        }
        or intent.get("schema_version")
        != _CONDITIONAL_PUBLICATION_SWAP_INTENT_SCHEMA
        or intent.get("status") != "prepared"
        or intent.get("operation_key") != operation_key
        or intent.get("filename") != filename
        or intent.get("candidate_name") != candidate_name
        or intent.get("candidate_sha256")
        != hashlib.sha256(content).hexdigest()
        or intent.get("candidate_bytes") != len(content)
        or intent.get("expected_target_precondition") != expected
    ):
        raise ValueError("conditional publication swap intent binding mismatch")
    try:
        prepared_at = datetime.fromisoformat(intent["prepared_at_utc"])
    except (TypeError, ValueError) as exc:
        raise ValueError("conditional publication swap timestamp is invalid") from exc
    if (
        prepared_at.tzinfo is None
        or prepared_at.utcoffset() != timedelta(0)
        or intent["prepared_at_utc"]
        != prepared_at.astimezone(timezone.utc).isoformat()
    ):
        raise ValueError("conditional publication swap timestamp is invalid")
    intent_sha256 = hashlib.sha256(
        _canonical_json_line_bytes(intent)
    ).hexdigest()

    def read_outcome() -> dict[str, Any] | None:
        outcome = _read_canonical_record_at(
            directory_fd,
            outcome_name,
            maximum_bytes=_MAX_CONDITIONAL_PUBLICATION_SWAP_RECORD_BYTES,
            label="conditional publication swap outcome",
        )
        if outcome is None:
            return None
        if (
            set(outcome)
            != {
                "schema_version", "status", "operation_key", "intent_sha256",
                "observed_target_precondition", "completed_at_utc",
            }
            or outcome.get("schema_version")
            != _CONDITIONAL_PUBLICATION_SWAP_OUTCOME_SCHEMA
            or outcome.get("status") not in {"published", "collision"}
            or outcome.get("operation_key") != operation_key
            or outcome.get("intent_sha256") != intent_sha256
        ):
            raise ValueError("conditional publication swap outcome mismatch")
        _validate_publication_target_precondition(
            outcome.get("observed_target_precondition")
        )
        try:
            completed = datetime.fromisoformat(outcome["completed_at_utc"])
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "conditional publication swap outcome timestamp is invalid"
            ) from exc
        if (
            completed.tzinfo is None
            or completed.utcoffset() != timedelta(0)
            or outcome["completed_at_utc"]
            != completed.astimezone(timezone.utc).isoformat()
        ):
            raise ValueError(
                "conditional publication swap outcome timestamp is invalid"
            )
        return outcome

    outcome = read_outcome()
    if outcome is not None:
        if outcome["status"] != "published":
            return None
        return _regular_content_matches_at(
            directory_fd, filename, content=content
        )

    candidate_record = _read_canonical_record_at(
        directory_fd,
        candidate_record_name,
        maximum_bytes=_MAX_CONDITIONAL_PUBLICATION_SWAP_RECORD_BYTES,
        label="conditional publication swap candidate",
    )
    if candidate_record is None:
        descriptor = -1
        try:
            try:
                descriptor = os.open(
                    candidate_name,
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                    dir_fd=directory_fd,
                )
                _write_all(descriptor, content)
                os.fsync(descriptor)
                metadata = os.fstat(descriptor)
            except FileExistsError:
                identity = _regular_content_matches_at(
                    directory_fd, candidate_name, content=content
                )
                if identity is None:
                    raise ValueError(
                        "conditional publication swap candidate collided"
                    )
                metadata = _lstat_at(directory_fd, candidate_name)
                assert metadata is not None
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        candidate_record = {
            "schema_version": _CONDITIONAL_PUBLICATION_SWAP_CANDIDATE_SCHEMA,
            "status": "durable",
            "operation_key": operation_key,
            "intent_sha256": intent_sha256,
            "st_dev": metadata.st_dev,
            "st_ino": metadata.st_ino,
            "candidate_sha256": hashlib.sha256(content).hexdigest(),
            "candidate_bytes": len(content),
        }
        candidate_record = _write_once_canonical_record_at(
            directory_fd,
            candidate_record_name,
            candidate_record,
            maximum_bytes=_MAX_CONDITIONAL_PUBLICATION_SWAP_RECORD_BYTES,
            label="conditional publication swap candidate",
        )
    if (
        set(candidate_record)
        != {
            "schema_version", "status", "operation_key", "intent_sha256",
            "st_dev", "st_ino", "candidate_sha256", "candidate_bytes",
        }
        or candidate_record.get("schema_version")
        != _CONDITIONAL_PUBLICATION_SWAP_CANDIDATE_SCHEMA
        or candidate_record.get("status") != "durable"
        or candidate_record.get("operation_key") != operation_key
        or candidate_record.get("intent_sha256") != intent_sha256
        or isinstance(candidate_record.get("st_dev"), bool)
        or not isinstance(candidate_record.get("st_dev"), int)
        or candidate_record["st_dev"] < 0
        or isinstance(candidate_record.get("st_ino"), bool)
        or not isinstance(candidate_record.get("st_ino"), int)
        or candidate_record["st_ino"] <= 0
        or candidate_record.get("candidate_sha256")
        != hashlib.sha256(content).hexdigest()
        or candidate_record.get("candidate_bytes") != len(content)
    ):
        raise ValueError("conditional publication swap candidate mismatch")
    candidate_identity = (
        candidate_record["st_dev"],
        candidate_record["st_ino"],
    )
    candidate_at_temp = _regular_content_matches_at(
        directory_fd,
        candidate_name,
        content=content,
        identity=candidate_identity,
    )
    candidate_at_target = _regular_content_matches_at(
        directory_fd,
        filename,
        content=content,
        identity=candidate_identity,
    )
    current = _publication_target_precondition_at(
        directory_fd,
        filename,
        display_path=display_path,
        maximum_bytes=maximum_target_bytes,
    )

    def commit_outcome(
        status_value: str, observed: Mapping[str, Any]
    ) -> dict[str, Any]:
        value = {
            "schema_version": _CONDITIONAL_PUBLICATION_SWAP_OUTCOME_SCHEMA,
            "status": status_value,
            "operation_key": operation_key,
            "intent_sha256": intent_sha256,
            "observed_target_precondition": dict(observed),
            "completed_at_utc": datetime.now(timezone.utc).isoformat(),
        }
        return _write_once_canonical_record_at(
            directory_fd,
            outcome_name,
            value,
            maximum_bytes=_MAX_CONDITIONAL_PUBLICATION_SWAP_RECORD_BYTES,
            label="conditional publication swap outcome",
        )

    def discard_candidate_name() -> None:
        metadata = _lstat_at(directory_fd, candidate_name)
        if metadata is not None:
            os.unlink(candidate_name, dir_fd=directory_fd)
            os.fsync(directory_fd)

    def settle_exchanged_target() -> tuple[int, int] | None:
        displaced = _publication_target_precondition_at(
            directory_fd,
            candidate_name,
            display_path=display_path.with_name(candidate_name),
            maximum_bytes=maximum_target_bytes,
        )
        if displaced == expected:
            commit_outcome("published", displaced)
            if not retain_displaced:
                discard_candidate_name()
            return _regular_content_matches_at(
                directory_fd,
                filename,
                content=content,
                identity=candidate_identity,
            )
        # Put the unexpected writer's object back.  If another writer raced
        # with this rollback, the exchange captures that newer object in the
        # candidate slot; exchange once more so the newest external object wins.
        if _regular_content_matches_at(
            directory_fd,
            filename,
            content=content,
            identity=candidate_identity,
        ) is not None:
            _renameat2_at(
                directory_fd, candidate_name, filename, _RENAME_EXCHANGE
            )
            captured = _regular_content_matches_at(
                directory_fd,
                candidate_name,
                content=content,
                identity=candidate_identity,
            )
            if captured is None:
                _renameat2_at(
                    directory_fd, candidate_name, filename, _RENAME_EXCHANGE
                )
        os.fsync(directory_fd)
        observed = _publication_target_precondition_at(
            directory_fd,
            filename,
            display_path=display_path,
            maximum_bytes=maximum_target_bytes,
        )
        commit_outcome("collision", observed)
        discard_candidate_name()
        return None

    if candidate_at_target is not None:
        if candidate_at_temp is not None:
            raise ValueError(
                "conditional publication swap candidate appears twice"
            )
        if _lstat_at(directory_fd, candidate_name) is None:
            if expected["kind"] == "absent":
                os.fsync(directory_fd)
                commit_outcome("published", expected)
                return candidate_at_target
            raise ValueError(
                "conditional publication swap lost its displaced target"
            )
        return settle_exchanged_target()
    if candidate_at_temp is None:
        candidate_entry = _lstat_at(directory_fd, candidate_name)
        displaced_matches_expected = (
            expected["kind"] == "absent" and candidate_entry is None
        )
        if candidate_entry is not None:
            displaced_matches_expected = (
                _publication_target_precondition_at(
                    directory_fd,
                    candidate_name,
                    display_path=display_path.with_name(candidate_name),
                    maximum_bytes=maximum_target_bytes,
                )
                == expected
            )
        if displaced_matches_expected:
            # The conditional rename completed, then a later ordinary writer
            # replaced our candidate before the terminal outcome was durable.
            # That later writer wins; retain it and settle the old attempt as
            # a collision without attempting another exchange.
            os.fsync(directory_fd)
            commit_outcome("collision", current)
            discard_candidate_name()
            return None
        raise ValueError("conditional publication swap candidate is unavailable")
    if current != expected:
        commit_outcome("collision", current)
        discard_candidate_name()
        return None
    if expected["kind"] == "absent":
        try:
            _renameat2_at(
                directory_fd, candidate_name, filename, _RENAME_NOREPLACE
            )
        except OSError as exc:
            if exc.errno not in {errno.EEXIST, errno.ENOTEMPTY}:
                raise
            observed = _publication_target_precondition_at(
                directory_fd,
                filename,
                display_path=display_path,
                maximum_bytes=maximum_target_bytes,
            )
            commit_outcome("collision", observed)
            discard_candidate_name()
            return None
        os.fsync(directory_fd)
        commit_outcome("published", expected)
        return _regular_content_matches_at(
            directory_fd,
            filename,
            content=content,
            identity=candidate_identity,
        )
    try:
        _renameat2_at(directory_fd, candidate_name, filename, _RENAME_EXCHANGE)
    except OSError:
        # A non-cooperating writer may remove or replace the expected target
        # after the precondition check but before renameat2(2).  If our exact
        # durable candidate is still present, that failure is a bounded target
        # collision rather than an indeterminate publication attempt.
        candidate_still_durable = _regular_content_matches_at(
            directory_fd,
            candidate_name,
            content=content,
            identity=candidate_identity,
        )
        observed = _publication_target_precondition_at(
            directory_fd,
            filename,
            display_path=display_path,
            maximum_bytes=maximum_target_bytes,
        )
        if candidate_still_durable is not None and observed != expected:
            commit_outcome("collision", observed)
            discard_candidate_name()
            return None
        raise
    os.fsync(directory_fd)
    return settle_exchanged_target()


def _finalize_retained_conditional_replace_at(
    directory_fd: int,
    filename: str,
    content: bytes,
    *,
    expected_precondition: Mapping[str, Any],
    published_identity: tuple[int, int],
    display_path: Path,
) -> None:
    expected = _validate_publication_target_precondition(
        dict(expected_precondition)
    )
    (
        _operation_key,
        _intent_name,
        _candidate_record_name,
        _outcome_name,
        candidate_name,
    ) = _conditional_publication_swap_paths(filename, expected, content)
    if _regular_content_matches_at(
        directory_fd,
        filename,
        content=content,
        identity=published_identity,
    ) is None:
        raise ValueError(f"published target changed before commit: {display_path}")
    candidate = _lstat_at(directory_fd, candidate_name)
    if candidate is None:
        # A durable ``published`` outcome plus the exact published inode proves
        # that the exchange completed.  For a non-absent predecessor the
        # exchange necessarily placed that predecessor at ``candidate_name``;
        # its later absence is therefore the idempotent post-commit cleanup
        # state (including a crash just after unlink+directory fsync).
        return
    displaced = _publication_target_precondition_at(
        directory_fd,
        candidate_name,
        display_path=display_path.with_name(candidate_name),
    )
    if displaced != expected:
        raise ValueError("conditional publication retained target changed")
    os.unlink(candidate_name, dir_fd=directory_fd)
    os.fsync(directory_fd)


def _rollback_retained_conditional_replace_at(
    directory_fd: int,
    filename: str,
    content: bytes,
    *,
    expected_precondition: Mapping[str, Any],
    published_identity: tuple[int, int],
    display_path: Path,
) -> Mapping[str, Any]:
    expected = _validate_publication_target_precondition(
        dict(expected_precondition)
    )
    (
        _operation_key,
        _intent_name,
        _candidate_record_name,
        _outcome_name,
        candidate_name,
    ) = _conditional_publication_swap_paths(filename, expected, content)
    target_is_ours = _regular_content_matches_at(
        directory_fd,
        filename,
        content=content,
        identity=published_identity,
    )
    if target_is_ours is None:
        candidate = _lstat_at(directory_fd, candidate_name)
        current = _publication_target_precondition_at(
            directory_fd, filename, display_path=display_path
        )
        if candidate is not None:
            candidate_is_ours = _regular_content_matches_at(
                directory_fd,
                candidate_name,
                content=content,
                identity=published_identity,
            )
            candidate_precondition = _publication_target_precondition_at(
                directory_fd,
                candidate_name,
                display_path=display_path.with_name(candidate_name),
            )
            if (
                current == expected
                and candidate_is_ours is None
                and candidate_precondition != expected
            ):
                # Recovery after the first half of the two-exchange race
                # rollback: target holds the old displaced object while the
                # candidate slot holds the later writer.  Exchange once more
                # so the later writer wins and the old object becomes cleanup.
                _renameat2_at(
                    directory_fd,
                    candidate_name,
                    filename,
                    _RENAME_EXCHANGE,
                )
                os.fsync(directory_fd)
                candidate_precondition = _publication_target_precondition_at(
                    directory_fd,
                    candidate_name,
                    display_path=display_path.with_name(candidate_name),
                )
                if candidate_precondition != expected:
                    raise ValueError(
                        "conditional publication rollback recovery mismatch"
                    )
            os.unlink(candidate_name, dir_fd=directory_fd)
            os.fsync(directory_fd)
        return _publication_target_precondition_at(
            directory_fd, filename, display_path=display_path
        )
    if expected["kind"] == "absent":
        _unlink_if_identity_at(directory_fd, filename, published_identity)
        return _publication_target_precondition_at(
            directory_fd, filename, display_path=display_path
        )
    displaced = _publication_target_precondition_at(
        directory_fd,
        candidate_name,
        display_path=display_path.with_name(candidate_name),
    )
    if displaced != expected:
        raise ValueError("conditional publication cannot restore retained target")
    _renameat2_at(directory_fd, candidate_name, filename, _RENAME_EXCHANGE)
    os.fsync(directory_fd)
    captured = _regular_content_matches_at(
        directory_fd,
        candidate_name,
        content=content,
        identity=published_identity,
    )
    if captured is None:
        # A later ordinary writer replaced our candidate after the optimistic
        # target check but before the exchange.  Put that newer writer back;
        # the second exchange leaves the original displaced object in the
        # candidate slot, where it can be discarded safely.
        _renameat2_at(
            directory_fd, candidate_name, filename, _RENAME_EXCHANGE
        )
        os.fsync(directory_fd)
        restored_candidate = _publication_target_precondition_at(
            directory_fd,
            candidate_name,
            display_path=display_path.with_name(candidate_name),
        )
        if restored_candidate != expected:
            raise ValueError(
                "conditional publication rollback could not restore later writer"
            )
    os.unlink(candidate_name, dir_fd=directory_fd)
    os.fsync(directory_fd)
    return _publication_target_precondition_at(
        directory_fd, filename, display_path=display_path
    )


def _unlink_if_identity_at(
    directory_fd: int,
    filename: str,
    identity: tuple[int, int],
) -> None:
    metadata = _lstat_at(directory_fd, filename)
    if metadata is None or (metadata.st_dev, metadata.st_ino) != identity:
        return
    os.unlink(filename, dir_fd=directory_fd)
    os.fsync(directory_fd)


def _unlink_path_if_identity(path: Path, identity: tuple[int, int]) -> None:
    path = _absolute_path(path)
    try:
        directory_fd = _open_directory(path.parent, label="receipt parent")
    except ValueError:
        return
    try:
        _unlink_if_identity_at(directory_fd, path.name, identity)
    finally:
        os.close(directory_fd)


def _write_receipt_atomic_at(
    directory_fd: int,
    path: Path,
    payload: Dict[str, Any],
    *,
    maximum_bytes: int | None = None,
) -> tuple[int, int]:
    path = _absolute_path(path)
    metadata = _lstat_at(directory_fd, path.name)
    if metadata is not None and stat.S_ISLNK(metadata.st_mode):
        raise ValueError(f"receipt target must not be a symlink: {path}")
    if metadata is not None and not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"receipt target must be a regular file: {path}")
    encoded = _canonical_json_line_bytes(payload)
    if maximum_bytes is None:
        maximum_bytes = MAX_PUBLICATION_RECEIPT_BYTES
    if (
        isinstance(maximum_bytes, bool)
        or not isinstance(maximum_bytes, int)
        or not 0 < maximum_bytes <= ABSOLUTE_MAX_PUBLICATION_RECEIPT_BYTES
        or len(encoded) > maximum_bytes
    ):
        raise ValueError("publication receipt exceeds its absolute byte limit")
    _assert_directory_binding(path.parent, directory_fd, label="receipt parent")
    # Check again immediately before replace so a symlink installed while
    # serializing is rejected rather than silently treated as a receipt.
    metadata = _lstat_at(directory_fd, path.name)
    if metadata is not None and stat.S_ISLNK(metadata.st_mode):
        raise ValueError(f"receipt target must not be a symlink: {path}")
    if metadata is not None and not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"receipt target must be a regular file: {path}")
    identity = _atomic_replace_at(directory_fd, path.name, encoded)
    try:
        _assert_directory_binding(path.parent, directory_fd, label="receipt parent")
    except ValueError:
        _unlink_if_identity_at(directory_fd, path.name, identity)
        raise
    return identity


def _write_receipt_atomic(
    path: Path,
    payload: Dict[str, Any],
) -> tuple[int, int]:
    path = _absolute_path(path)
    directory_fd = _open_or_create_directory_durable(
        path.parent, label="receipt parent"
    )
    try:
        return _write_receipt_atomic_at(directory_fd, path, payload)
    finally:
        os.close(directory_fd)


def _canonical_json_line_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _read_canonical_publication_receipt(
    path: Path,
) -> tuple[dict[str, Any], bytes] | None:
    """Read one exact regular canonical receipt without following symlinks."""

    path = _absolute_path(path)
    try:
        directory_fd = _open_directory(path.parent, label="receipt parent")
    except ValueError:
        try:
            path.parent.lstat()
        except FileNotFoundError:
            return None
        raise
    descriptor = -1
    try:
        try:
            descriptor = os.open(
                path.name,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=directory_fd,
            )
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise ValueError(
                f"publication receipt must be a regular file: {path}"
            ) from exc
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size > ABSOLUTE_MAX_PUBLICATION_RECEIPT_BYTES
        ):
            raise ValueError("publication receipt is unsafe or exceeds its cap")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            raw = handle.read(ABSOLUTE_MAX_PUBLICATION_RECEIPT_BYTES + 1)
        if len(raw) > ABSOLUTE_MAX_PUBLICATION_RECEIPT_BYTES:
            raise ValueError("publication receipt exceeds its absolute byte cap")
        _assert_directory_binding(path.parent, directory_fd, label="receipt parent")
        try:
            value = json.loads(raw.decode("utf-8"))
            canonical = _canonical_json_line_bytes(value)
        except (UnicodeError, TypeError, ValueError, RecursionError) as exc:
            raise ValueError("publication receipt is not canonical JSON") from exc
        if not isinstance(value, dict) or raw != canonical:
            raise ValueError("publication receipt is not canonical JSON")
        return value, raw
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(directory_fd)


def _read_canonical_publication_receipt_at(
    directory_fd: int, path: Path
) -> tuple[dict[str, Any], bytes] | None:
    value = _read_canonical_record_at(
        directory_fd,
        path.name,
        maximum_bytes=ABSOLUTE_MAX_PUBLICATION_RECEIPT_BYTES,
        label="publication receipt",
    )
    if value is None:
        return None
    raw = _canonical_json_line_bytes(value)
    return value, raw


def _validate_prepared_publication_receipt(
    receipt: Mapping[str, Any],
    *,
    receipt_bytes: bytes,
    statement: str,
    proof: str,
    proof_bytes: bytes,
    manifest: ProofManifest,
    expected_ids: list[str],
    expected_digest: str,
    expected_context_digest: str,
    verified_path: Path,
    problem_id: str,
    supersedes: list[dict[str, str]],
) -> dict[str, Any]:
    """Authenticate all receipt-first recovery inputs under persisted limits."""

    v4_keys = {
        "schema_version", "state", "problem_id", "statement_source_digest",
        "canonical_target_digest", "proof_digest", "context_digest",
        "adaptive_context_digest", "item_context_attestations",
        "checked_item_ids", "verified_path", "published_bytes",
        "published_at_utc", "verification_quorum", "verification_passes",
        "supersedes", "proof_context", "verification_limits",
    }
    schema = receipt.get("schema_version")
    if not (
        schema == "rethlas-publication-v4" and set(receipt) == v4_keys
        or schema == "rethlas-publication-v5"
        and set(receipt) == v4_keys | {"publication_target_precondition"}
        or schema == "rethlas-publication-v6"
        and set(receipt) == v4_keys | {"publication_target_precondition"}
    ):
        raise ValueError("prepared publication receipt shape mismatch")
    if schema in {"rethlas-publication-v5", "rethlas-publication-v6"}:
        _validate_publication_target_precondition(
            receipt["publication_target_precondition"]
        )
    limits = receipt.get("verification_limits")
    limit_keys = {
        "context_max_chars", "max_expansion_rounds", "max_expanded_proofs",
        "max_expanded_proof_chars", "max_proof_items", "max_receipt_bytes",
    }
    if schema == "rethlas-publication-v6":
        limit_keys |= {"max_blueprint_bytes", "max_blueprint_chars"}
    absolutes = {
        "context_max_chars": ABSOLUTE_VERIFY_CONTEXT_MAX_CHARS,
        "max_expansion_rounds": ABSOLUTE_MAX_EXPANSION_ROUNDS,
        "max_expanded_proofs": ABSOLUTE_MAX_EXPANDED_PROOFS,
        "max_expanded_proof_chars": ABSOLUTE_MAX_EXPANDED_PROOF_CHARS,
        "max_proof_items": ABSOLUTE_MAX_PUBLICATION_PROOF_ITEMS,
        "max_receipt_bytes": ABSOLUTE_MAX_PUBLICATION_RECEIPT_BYTES,
    }
    if schema == "rethlas-publication-v6":
        absolutes.update(
            {
                "max_blueprint_bytes": ABSOLUTE_MAX_BLUEPRINT_BYTES,
                "max_blueprint_chars": ABSOLUTE_MAX_BLUEPRINT_CHARS,
            }
        )
    if not isinstance(limits, dict) or set(limits) != limit_keys:
        raise ValueError("prepared publication limit profile mismatch")
    for field, maximum in absolutes.items():
        configured = limits.get(field)
        if (
            isinstance(configured, bool)
            or not isinstance(configured, int)
            or configured < 0
            or configured > maximum
        ):
            raise ValueError("prepared publication limit profile mismatch")
    if (
        limits["max_proof_items"] <= 0
        or limits["max_receipt_bytes"] <= 0
        or len(receipt_bytes) > limits["max_receipt_bytes"]
        or len(expected_ids) > limits["max_proof_items"]
        or (
            schema == "rethlas-publication-v6"
            and (
                len(proof_bytes) > limits["max_blueprint_bytes"]
                or len(proof) > limits["max_blueprint_chars"]
            )
        )
    ):
        raise ValueError("prepared publication exceeds its persisted limits")
    proof_context = receipt.get("proof_context")
    proof_context_keys = {
        "schema_version", "source_sha256", "proof_item_schema_version",
        "proof_context_schema_version", "aggregate_context_schema_version",
        "adaptive_aggregate_context_schema_version",
    }
    if (
        not isinstance(proof_context, dict)
        or set(proof_context) != proof_context_keys
        or proof_context.get("schema_version") != PUBLICATION_PROOF_CONTEXT_SCHEMA
        or proof_context.get("source_sha256")
        != _assert_publication_proof_context_unchanged()
        or proof_context.get("proof_item_schema_version")
        != PROOF_ITEM_SCHEMA_VERSION
        or proof_context.get("proof_context_schema_version")
        != PROOF_CONTEXT_SCHEMA_VERSION
        or proof_context.get("aggregate_context_schema_version")
        != AGGREGATE_CONTEXT_SCHEMA_VERSION
        or proof_context.get("adaptive_aggregate_context_schema_version")
        != ADAPTIVE_AGGREGATE_CONTEXT_SCHEMA_VERSION
    ):
        raise ValueError("prepared publication proof-context binding mismatch")
    checked = receipt.get("checked_item_ids")
    if (
        receipt.get("state") != "active"
        or receipt.get("problem_id") != problem_id
        or receipt.get("statement_source_digest") != proof_digest(statement)
        or receipt.get("canonical_target_digest")
        != proof_digest(extract_verification_target(statement))
        or receipt.get("proof_digest") != expected_digest
        or receipt.get("context_digest") != expected_context_digest
        or checked != expected_ids
        or receipt.get("verified_path") != str(verified_path)
        or isinstance(receipt.get("published_bytes"), bool)
        or receipt.get("published_bytes") != len(proof_bytes)
        or receipt.get("verification_quorum") != 2
        or receipt.get("supersedes") != supersedes
    ):
        raise ValueError("prepared publication receipt binding mismatch")
    published_at = receipt.get("published_at_utc")
    try:
        timestamp = datetime.fromisoformat(published_at)
    except (TypeError, ValueError) as exc:
        raise ValueError("prepared publication timestamp is invalid") from exc
    if (
        timestamp.tzinfo is None
        or timestamp.utcoffset() != timedelta(0)
        or published_at != timestamp.astimezone(timezone.utc).isoformat()
    ):
        raise ValueError("prepared publication timestamp is invalid")

    passes = receipt.get("verification_passes")
    pass_keys = {
        "pass_index", "verification_attempt_id", "verifier_run_id",
        "verifier_model", "verifier_reasoning_effort",
        "verifier_service_version", "verification_role", "response_sha256",
        "verdict",
    }
    if not isinstance(passes, list) or len(passes) != 2:
        raise ValueError("prepared publication verifier quorum mismatch")
    attempts: list[str] = []
    runs: list[str] = []
    for index, verification_pass in enumerate(passes, 1):
        expected_role = (
            "primary" if index == 1 else "adversarial_full_claim_audit"
        )
        if (
            not isinstance(verification_pass, dict)
            or set(verification_pass) != pass_keys
            or verification_pass.get("pass_index") != index
            or isinstance(verification_pass.get("pass_index"), bool)
            or not isinstance(verification_pass.get("verification_attempt_id"), str)
            or _VERIFICATION_ATTEMPT_RE.fullmatch(
                verification_pass["verification_attempt_id"]
            )
            is None
            or not isinstance(verification_pass.get("verifier_run_id"), str)
            or _VERIFIER_RUN_ID_RE.fullmatch(verification_pass["verifier_run_id"])
            is None
            or not isinstance(verification_pass.get("verifier_model"), str)
            or not verification_pass["verifier_model"].strip()
            or verification_pass.get("verifier_reasoning_effort")
            not in {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}
            or not isinstance(
                verification_pass.get("verifier_service_version"), str
            )
            or not verification_pass["verifier_service_version"].strip()
            or verification_pass.get("verification_role") != expected_role
            or not isinstance(verification_pass.get("response_sha256"), str)
            or _HEX_DIGEST_RE.fullmatch(verification_pass["response_sha256"])
            is None
            or verification_pass.get("verdict") != "correct"
        ):
            raise ValueError("prepared publication verifier pass mismatch")
        attempts.append(verification_pass["verification_attempt_id"])
        runs.append(verification_pass["verifier_run_id"])
    if len(set(attempts)) != 2 or len(set(runs)) != 2:
        raise ValueError("prepared publication verifier passes are not independent")

    for prior in supersedes:
        if (
            not isinstance(prior, dict)
            or set(prior) != {"problem_id", "receipt_sha256", "proof_digest"}
            or not isinstance(prior["problem_id"], str)
            or not prior["problem_id"]
            or _HEX_DIGEST_RE.fullmatch(prior["receipt_sha256"]) is None
            or _HEX_DIGEST_RE.fullmatch(prior["proof_digest"]) is None
        ):
            raise ValueError("prepared publication supersedes binding mismatch")

    attestations = receipt.get("item_context_attestations")
    if not isinstance(attestations, list) or len(attestations) != len(expected_ids):
        raise ValueError("prepared publication item coverage mismatch")
    rebuilt_attestations: list[dict[str, Any]] = []
    for index, (item_id, attestation) in enumerate(
        zip(expected_ids, attestations, strict=True)
    ):
        if (
            not isinstance(attestation, dict)
            or set(attestation) != _ITEM_CONTEXT_ATTESTATION_FIELDS
            or attestation.get("item_id") != item_id
            or attestation.get("disposition") != "verified"
            or attestation.get("verdict") != "correct"
        ):
            raise ValueError("prepared publication item attestation mismatch")
        final_round = attestation.get("final_round")
        expanded_ids = attestation.get("expanded_proof_ids")
        max_chars = attestation.get("max_chars")
        context_digest = attestation.get("context_digest")
        if (
            isinstance(final_round, bool)
            or not isinstance(final_round, int)
            or not 0 <= final_round <= limits["max_expansion_rounds"]
            or not isinstance(expanded_ids, list)
            or len(expanded_ids) > limits["max_expanded_proofs"]
            or len(set(expanded_ids)) != len(expanded_ids)
            or any(
                not isinstance(expanded_id, str)
                or _ITEM_ID_RE.fullmatch(expanded_id) is None
                for expanded_id in expanded_ids
            )
            or (final_round == 0) != (expanded_ids == [])
            or isinstance(max_chars, bool)
            or not isinstance(max_chars, int)
            or not 0 < max_chars <= limits["context_max_chars"]
            or not isinstance(context_digest, str)
            or _HEX_DIGEST_RE.fullmatch(context_digest) is None
        ):
            raise ValueError("prepared publication item attestation mismatch")
        try:
            rebuilt = build_item_context(
                manifest,
                item_id,
                max_chars=max_chars,
                expanded_proof_ids=expanded_ids,
                round_index=final_round,
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "prepared publication item context cannot be rebuilt"
            ) from exc
        if (
            rebuilt["complete"] is not True
            or rebuilt["truncated"] is not False
            or rebuilt["missing"]
            or rebuilt["omitted"]
            or rebuilt["digest"] != context_digest
            or rebuilt["expanded_proof_characters"]
            > limits["max_expanded_proof_chars"]
        ):
            raise ValueError("prepared publication item context mismatch")
        rebuilt_attestations.append(dict(attestation))
    if receipt.get("adaptive_context_digest") != aggregate_adaptive_context_digest(
        manifest, rebuilt_attestations
    ):
        raise ValueError("prepared publication adaptive context mismatch")
    return dict(receipt)


def _prepared_publication_result(
    *, receipt: Mapping[str, Any], verified_path: Path, receipt_path: Path
) -> Dict[str, Any]:
    return {
        "published": True,
        "published_path": str(verified_path),
        "publication_receipt_path": str(receipt_path),
        "verdict": "correct",
        "verification_status": "final",
        "proof_digest": receipt["proof_digest"],
        "context_digest": receipt["context_digest"],
        "adaptive_context_digest": receipt["adaptive_context_digest"],
        "checked_item_ids": receipt["checked_item_ids"],
        "item_context_attestations": receipt["item_context_attestations"],
        "verification_quorum": receipt["verification_quorum"],
        "verification_passes": receipt["verification_passes"],
        "recovered_prepared_publication": True,
    }


def _prepared_publication_receipt_identity(
    receipt: Mapping[str, Any],
    *,
    receipt_bytes: bytes,
    problem_id: str,
    verified_path: Path,
) -> dict[str, Any]:
    """Validate the receipt-owned identity without requiring old draft bytes."""

    v4_keys = {
        "schema_version", "state", "problem_id", "statement_source_digest",
        "canonical_target_digest", "proof_digest", "context_digest",
        "adaptive_context_digest", "item_context_attestations",
        "checked_item_ids", "verified_path", "published_bytes",
        "published_at_utc", "verification_quorum", "verification_passes",
        "supersedes", "proof_context", "verification_limits",
    }
    schema = receipt.get("schema_version")
    if not (
        schema == "rethlas-publication-v4" and set(receipt) == v4_keys
        or schema == "rethlas-publication-v5"
        and set(receipt) == v4_keys | {"publication_target_precondition"}
        or schema == "rethlas-publication-v6"
        and set(receipt) == v4_keys | {"publication_target_precondition"}
    ):
        raise ValueError("prepared publication receipt shape mismatch")
    digest_fields = (
        "statement_source_digest",
        "canonical_target_digest",
        "proof_digest",
        "context_digest",
        "adaptive_context_digest",
    )
    checked = receipt.get("checked_item_ids")
    attestations = receipt.get("item_context_attestations")
    if (
        receipt.get("state") != "active"
        or receipt.get("problem_id") != problem_id
        or receipt.get("verified_path") != str(verified_path)
        or any(
            not isinstance(receipt.get(field), str)
            or _HEX_DIGEST_RE.fullmatch(receipt[field]) is None
            for field in digest_fields
        )
        or isinstance(receipt.get("published_bytes"), bool)
        or not isinstance(receipt.get("published_bytes"), int)
        or receipt["published_bytes"] <= 0
        or receipt.get("verification_quorum") != 2
        or isinstance(receipt.get("verification_quorum"), bool)
        or not isinstance(receipt.get("verification_passes"), list)
        or len(receipt["verification_passes"]) != 2
        or not isinstance(receipt.get("supersedes"), list)
        or len(receipt["supersedes"]) > 1
        or not isinstance(checked, list)
        or not checked
        or any(
            not isinstance(item_id, str)
            or _ITEM_ID_RE.fullmatch(item_id) is None
            for item_id in checked
        )
        or len(set(checked)) != len(checked)
        or not isinstance(attestations, list)
        or len(attestations) != len(checked)
    ):
        raise ValueError("prepared publication receipt identity mismatch")
    try:
        published = datetime.fromisoformat(receipt["published_at_utc"])
    except (TypeError, ValueError) as exc:
        raise ValueError("prepared publication timestamp is invalid") from exc
    if (
        published.tzinfo is None
        or published.utcoffset() != timedelta(0)
        or receipt["published_at_utc"]
        != published.astimezone(timezone.utc).isoformat()
    ):
        raise ValueError("prepared publication timestamp is invalid")
    limits = receipt.get("verification_limits")
    limit_keys = {
        "context_max_chars", "max_expansion_rounds", "max_expanded_proofs",
        "max_expanded_proof_chars", "max_proof_items", "max_receipt_bytes",
    }
    if schema == "rethlas-publication-v6":
        limit_keys |= {"max_blueprint_bytes", "max_blueprint_chars"}
    absolutes = {
        "context_max_chars": ABSOLUTE_VERIFY_CONTEXT_MAX_CHARS,
        "max_expansion_rounds": ABSOLUTE_MAX_EXPANSION_ROUNDS,
        "max_expanded_proofs": ABSOLUTE_MAX_EXPANDED_PROOFS,
        "max_expanded_proof_chars": ABSOLUTE_MAX_EXPANDED_PROOF_CHARS,
        "max_proof_items": ABSOLUTE_MAX_PUBLICATION_PROOF_ITEMS,
        "max_receipt_bytes": ABSOLUTE_MAX_PUBLICATION_RECEIPT_BYTES,
    }
    if schema == "rethlas-publication-v6":
        absolutes.update(
            {
                "max_blueprint_bytes": ABSOLUTE_MAX_BLUEPRINT_BYTES,
                "max_blueprint_chars": ABSOLUTE_MAX_BLUEPRINT_CHARS,
            }
        )
    if not isinstance(limits, dict) or set(limits) != limit_keys:
        raise ValueError("prepared publication limit profile mismatch")
    if any(
        isinstance(limits[field], bool)
        or not isinstance(limits[field], int)
        or limits[field] < 0
        or limits[field] > maximum
        for field, maximum in absolutes.items()
    ) or (
        limits["max_proof_items"] <= 0
        or limits["max_receipt_bytes"] <= 0
        or len(receipt_bytes) > limits["max_receipt_bytes"]
        or len(checked) > limits["max_proof_items"]
        or (
            schema == "rethlas-publication-v6"
            and (
                limits["max_blueprint_bytes"] <= 0
                or limits["max_blueprint_chars"] <= 0
                or receipt["published_bytes"]
                > limits["max_blueprint_bytes"]
            )
        )
    ):
        raise ValueError("prepared publication limit profile mismatch")
    proof_context = receipt.get("proof_context")
    if (
        not isinstance(proof_context, dict)
        or set(proof_context)
        != {
            "schema_version", "source_sha256", "proof_item_schema_version",
            "proof_context_schema_version", "aggregate_context_schema_version",
            "adaptive_aggregate_context_schema_version",
        }
        or proof_context.get("schema_version")
        != PUBLICATION_PROOF_CONTEXT_SCHEMA
        or not isinstance(proof_context.get("source_sha256"), str)
        or _HEX_DIGEST_RE.fullmatch(proof_context["source_sha256"]) is None
        or proof_context.get("proof_item_schema_version") != 1
        or proof_context.get("proof_context_schema_version") != 2
        or proof_context.get("aggregate_context_schema_version") != 1
        or proof_context.get("adaptive_aggregate_context_schema_version") != 2
    ):
        raise ValueError("prepared publication proof-context identity mismatch")
    pass_keys = {
        "pass_index", "verification_attempt_id", "verifier_run_id",
        "verifier_model", "verifier_reasoning_effort",
        "verifier_service_version", "verification_role", "response_sha256",
        "verdict",
    }
    attempts: list[str] = []
    runs: list[str] = []
    for index, verification_pass in enumerate(
        receipt["verification_passes"], 1
    ):
        expected_role = (
            "primary" if index == 1 else "adversarial_full_claim_audit"
        )
        if (
            not isinstance(verification_pass, dict)
            or set(verification_pass) != pass_keys
            or verification_pass.get("pass_index") != index
            or isinstance(verification_pass.get("pass_index"), bool)
            or not isinstance(
                verification_pass.get("verification_attempt_id"), str
            )
            or _VERIFICATION_ATTEMPT_RE.fullmatch(
                verification_pass["verification_attempt_id"]
            )
            is None
            or not isinstance(verification_pass.get("verifier_run_id"), str)
            or _VERIFIER_RUN_ID_RE.fullmatch(
                verification_pass["verifier_run_id"]
            )
            is None
            or not isinstance(verification_pass.get("verifier_model"), str)
            or not verification_pass["verifier_model"].strip()
            or verification_pass.get("verifier_reasoning_effort")
            not in {
                "none", "minimal", "low", "medium", "high", "xhigh",
                "max", "ultra",
            }
            or not isinstance(
                verification_pass.get("verifier_service_version"), str
            )
            or not verification_pass["verifier_service_version"].strip()
            or verification_pass.get("verification_role") != expected_role
            or not isinstance(verification_pass.get("response_sha256"), str)
            or _HEX_DIGEST_RE.fullmatch(
                verification_pass["response_sha256"]
            )
            is None
            or verification_pass.get("verdict") != "correct"
        ):
            raise ValueError("prepared publication verifier pass mismatch")
        attempts.append(verification_pass["verification_attempt_id"])
        runs.append(verification_pass["verifier_run_id"])
    if len(set(attempts)) != 2 or len(set(runs)) != 2:
        raise ValueError("prepared publication verifier passes are not independent")
    for item_id, attestation in zip(checked, attestations, strict=True):
        if (
            not isinstance(attestation, dict)
            or set(attestation) != _ITEM_CONTEXT_ATTESTATION_FIELDS
            or attestation.get("item_id") != item_id
            or attestation.get("disposition") != "verified"
            or attestation.get("verdict") != "correct"
        ):
            raise ValueError("prepared publication item attestation mismatch")
        final_round = attestation.get("final_round")
        expanded_ids = attestation.get("expanded_proof_ids")
        max_chars = attestation.get("max_chars")
        context_digest = attestation.get("context_digest")
        if (
            isinstance(final_round, bool)
            or not isinstance(final_round, int)
            or not 0 <= final_round <= limits["max_expansion_rounds"]
            or not isinstance(expanded_ids, list)
            or len(expanded_ids) > limits["max_expanded_proofs"]
            or any(
                not isinstance(expanded_id, str)
                or _ITEM_ID_RE.fullmatch(expanded_id) is None
                for expanded_id in expanded_ids
            )
            or len(set(expanded_ids)) != len(expanded_ids)
            or (final_round == 0) != (expanded_ids == [])
            or isinstance(max_chars, bool)
            or not isinstance(max_chars, int)
            or not 0 < max_chars <= limits["context_max_chars"]
            or not isinstance(context_digest, str)
            or _HEX_DIGEST_RE.fullmatch(context_digest) is None
        ):
            raise ValueError("prepared publication item attestation mismatch")
    supersedes = receipt.get("supersedes")
    for prior in supersedes:
        if (
            not isinstance(prior, dict)
            or set(prior) != {"problem_id", "receipt_sha256", "proof_digest"}
            or not isinstance(prior.get("problem_id"), str)
            or not prior["problem_id"]
            or not isinstance(prior.get("receipt_sha256"), str)
            or _HEX_DIGEST_RE.fullmatch(prior["receipt_sha256"]) is None
            or not isinstance(prior.get("proof_digest"), str)
            or _HEX_DIGEST_RE.fullmatch(prior["proof_digest"]) is None
        ):
            raise ValueError("prepared publication supersedes identity mismatch")
    target_precondition = None
    if schema in {"rethlas-publication-v5", "rethlas-publication-v6"}:
        target_precondition = _validate_publication_target_precondition(
            receipt["publication_target_precondition"]
        )
    return {
        "problem_id": problem_id,
        "statement_source_digest": receipt["statement_source_digest"],
        "canonical_target_digest": receipt["canonical_target_digest"],
        "proof_digest": receipt["proof_digest"],
        "context_digest": receipt["context_digest"],
        "proof_context_source_sha256": proof_context["source_sha256"],
        "supersedes": [dict(prior) for prior in supersedes],
        "verified_path": str(verified_path),
        "receipt_sha256": hashlib.sha256(receipt_bytes).hexdigest(),
        "receipt_bytes": len(receipt_bytes),
        "persisted_target_precondition": target_precondition,
    }


def _prepared_publication_settlement_path(
    receipt_path: Path, receipt_sha256: str
) -> Path:
    if _HEX_DIGEST_RE.fullmatch(receipt_sha256) is None:
        raise ValueError("prepared publication receipt digest is invalid")
    return receipt_path.with_name(
        f".rethlas-prepared-publication-{receipt_sha256}.settlement.json"
    )


_PREPARED_PUBLICATION_ARCHIVE_IDENTITY_FIELDS = (
    "problem_id",
    "statement_source_digest",
    "canonical_target_digest",
    "proof_digest",
    "context_digest",
    "proof_context_source_sha256",
    "supersedes",
    "verified_path",
)
_PREPARED_PUBLICATION_ARCHIVE_KEY_FIELDS = (
    "problem_id",
    "statement_source_digest",
    "canonical_target_digest",
    "proof_digest",
    "supersedes",
    "verified_path",
)


def _prepared_publication_archive_identity(
    *,
    problem_id: str,
    statement: str,
    proof_sha256: str,
    context_digest: str,
    proof_context_source_sha256: str,
    supersedes: list[dict[str, str]],
    verified_path: Path,
) -> dict[str, Any]:
    return {
        "problem_id": problem_id,
        "statement_source_digest": proof_digest(statement),
        "canonical_target_digest": proof_digest(
            extract_verification_target(statement)
        ),
        "proof_digest": proof_sha256,
        "context_digest": context_digest,
        "proof_context_source_sha256": proof_context_source_sha256,
        "supersedes": [dict(value) for value in supersedes],
        "verified_path": str(verified_path),
    }


def _prepared_publication_archive_path(
    state_parent: Path,
    identity: Mapping[str, Any],
) -> Path:
    """Return the receipt-identity slot used to discover interrupted work.

    The slot deliberately excludes deployment/source bindings so that a later
    deployment can find and settle older unresolved evidence.  Once that
    evidence has a durable terminal settlement, the slot may advance to a new
    fully bound generation; the retired receipt is copied to the immutable
    generation path first.
    """
    key = hashlib.sha256(
        _canonical_json_line_bytes(
            {
                "schema_version": "rethlas_prepared_publication_archive_key_v1",
                **{
                    field: identity.get(field)
                    for field in _PREPARED_PUBLICATION_ARCHIVE_KEY_FIELDS
                },
            }
        )
    ).hexdigest()
    return state_parent / f".rethlas-prepared-receipt-{key}.json"


def _prepared_publication_archive_generation_path(
    state_parent: Path,
    identity: Mapping[str, Any],
) -> Path:
    key = hashlib.sha256(
        _canonical_json_line_bytes(
            {
                "schema_version": (
                    "rethlas_prepared_publication_archive_generation_key_v1"
                ),
                **{
                    field: identity.get(field)
                    for field in _PREPARED_PUBLICATION_ARCHIVE_IDENTITY_FIELDS
                },
                # The same complete source/context identity can be verified in
                # multiple publication generations.  Bind retired evidence to
                # its exact receipt, not merely to the deployment identity.
                "receipt_sha256": identity.get("receipt_sha256"),
            }
        )
    ).hexdigest()
    return state_parent / f".rethlas-prepared-receipt-generation-{key}.json"


def _persisted_publication_receipt_max_bytes(
    receipt: Mapping[str, Any],
) -> int:
    limits = receipt.get("verification_limits")
    maximum = (
        limits.get("max_receipt_bytes")
        if isinstance(limits, Mapping)
        else None
    )
    if (
        isinstance(maximum, bool)
        or not isinstance(maximum, int)
        or not 0 < maximum <= ABSOLUTE_MAX_PUBLICATION_RECEIPT_BYTES
    ):
        raise ValueError("prepared publication receipt cap is invalid")
    return maximum


def _persisted_publication_blueprint_limits(
    receipt: Mapping[str, Any],
) -> tuple[int, int]:
    schema = receipt.get("schema_version")
    limits = receipt.get("verification_limits")
    if schema == "rethlas-publication-v6":
        maximum_bytes = (
            limits.get("max_blueprint_bytes")
            if isinstance(limits, Mapping)
            else None
        )
        maximum_chars = (
            limits.get("max_blueprint_chars")
            if isinstance(limits, Mapping)
            else None
        )
    elif schema in {"rethlas-publication-v4", "rethlas-publication-v5"}:
        published_bytes = receipt.get("published_bytes")
        target = receipt.get("publication_target_precondition")
        target_bytes = (
            target.get("st_size")
            if isinstance(target, Mapping) and target.get("kind") == "regular"
            else 0
        )
        if (
            isinstance(published_bytes, bool)
            or not isinstance(published_bytes, int)
            or isinstance(target_bytes, bool)
            or not isinstance(target_bytes, int)
        ):
            raise ValueError("prepared publication blueprint cap is invalid")
        maximum_bytes = max(published_bytes, target_bytes, 1)
        # Historical receipts did not persist the character admission.  Their
        # exact content is still bounded by the stable absolute migration cap.
        maximum_chars = ABSOLUTE_MAX_BLUEPRINT_CHARS
    else:
        raise ValueError("prepared publication blueprint cap is invalid")
    if (
        isinstance(maximum_bytes, bool)
        or not isinstance(maximum_bytes, int)
        or not 0 < maximum_bytes <= ABSOLUTE_MAX_BLUEPRINT_BYTES
        or isinstance(maximum_chars, bool)
        or not isinstance(maximum_chars, int)
        or not 0 < maximum_chars <= ABSOLUTE_MAX_BLUEPRINT_CHARS
    ):
        raise ValueError("prepared publication blueprint cap is invalid")
    return maximum_bytes, maximum_chars


def _read_prepared_publication_archive(
    *,
    state_parent: Path,
    state_parent_fd: int,
    identity: Mapping[str, Any],
    path: Path | None = None,
    require_full_identity: bool = False,
) -> tuple[dict[str, Any], bytes] | None:
    if path is None:
        path = _prepared_publication_archive_path(state_parent, identity)
    value = _read_direct_finalization_record(
        path,
        maximum_bytes=_ABSOLUTE_MAX_PREPARED_PUBLICATION_ARCHIVE_BYTES,
        directory_fd=state_parent_fd,
    )
    if value is None:
        return None
    legacy_keys = {
        "schema_version",
        "status",
        *_PREPARED_PUBLICATION_ARCHIVE_IDENTITY_FIELDS,
        "receipt_sha256",
        "receipt_bytes",
        "receipt",
        "archived_at_utc",
    }
    receipt = value.get("receipt")
    if not isinstance(receipt, dict):
        raise ValueError("prepared publication archive receipt is invalid")
    receipt_bytes = _canonical_json_line_bytes(receipt)
    persisted_receipt_max = _persisted_publication_receipt_max_bytes(receipt)
    schema = value.get("schema_version")
    if schema == _PREPARED_PUBLICATION_ARCHIVE_SCHEMA_LEGACY:
        expected_keys = legacy_keys
        persisted_archive_max = (
            _ABSOLUTE_MAX_PREPARED_PUBLICATION_ARCHIVE_BYTES
        )
    elif schema == _PREPARED_PUBLICATION_ARCHIVE_SCHEMA:
        expected_keys = legacy_keys | {
            "max_receipt_bytes",
            "max_archive_bytes",
        }
        persisted_archive_max = value.get("max_archive_bytes")
        if (
            value.get("max_receipt_bytes") != persisted_receipt_max
            or isinstance(persisted_archive_max, bool)
            or not isinstance(persisted_archive_max, int)
            or not 0 < persisted_archive_max
            <= _ABSOLUTE_MAX_PREPARED_PUBLICATION_ARCHIVE_BYTES
        ):
            raise ValueError("prepared publication archive cap mismatch")
    else:
        raise ValueError("prepared publication archive schema mismatch")
    if (
        set(value) != expected_keys
        or value.get("status") != "prepared"
        or any(
            value.get(field) != identity.get(field)
            for field in (
                _PREPARED_PUBLICATION_ARCHIVE_IDENTITY_FIELDS
                if require_full_identity
                else _PREPARED_PUBLICATION_ARCHIVE_KEY_FIELDS
            )
        )
        or value.get("receipt_sha256")
        != hashlib.sha256(receipt_bytes).hexdigest()
        or value.get("receipt_bytes") != len(receipt_bytes)
        or len(receipt_bytes) > persisted_receipt_max
        or len(_canonical_json_line_bytes(value)) > persisted_archive_max
    ):
        raise ValueError("prepared publication archive binding mismatch")
    archived_receipt_identity = _prepared_publication_receipt_identity(
        receipt,
        receipt_bytes=receipt_bytes,
        problem_id=str(identity["problem_id"]),
        verified_path=Path(str(identity["verified_path"])),
    )
    if any(
        value.get(field) != archived_receipt_identity.get(field)
        for field in _PREPARED_PUBLICATION_ARCHIVE_IDENTITY_FIELDS
    ):
        raise ValueError("prepared publication archive receipt binding mismatch")
    try:
        archived = datetime.fromisoformat(value["archived_at_utc"])
    except (TypeError, ValueError) as exc:
        raise ValueError("prepared publication archive timestamp is invalid") from exc
    if (
        archived.tzinfo is None
        or archived.utcoffset() != timedelta(0)
        or value["archived_at_utc"]
        != archived.astimezone(timezone.utc).isoformat()
    ):
        raise ValueError("prepared publication archive timestamp is invalid")
    return receipt, receipt_bytes


def _commit_prepared_publication_archive(
    *,
    state_parent: Path,
    state_parent_fd: int,
    identity: Mapping[str, Any],
    receipt: dict[str, Any],
    receipt_path: Path,
) -> tuple[dict[str, Any], bytes]:
    existing = _read_prepared_publication_archive(
        state_parent=state_parent,
        state_parent_fd=state_parent_fd,
        identity=identity,
    )
    receipt_bytes = _canonical_json_line_bytes(receipt)
    expected = (receipt, receipt_bytes)
    if existing is not None:
        if existing == expected:
            return existing
        retired_receipt, retired_receipt_bytes = existing
        retired_identity = _prepared_publication_receipt_identity(
            retired_receipt,
            receipt_bytes=retired_receipt_bytes,
            problem_id=str(identity["problem_id"]),
            verified_path=Path(str(identity["verified_path"])),
        )
        retired_settlement = _read_prepared_publication_settlement(
            receipt_path=receipt_path,
            identity=retired_identity,
            state_parent_fd=state_parent_fd,
        )
        if retired_settlement is None:
            raise ValueError("prepared publication archive changed on replay")

        # Preserve the retired verifier evidence under its complete immutable
        # source/context identity before advancing the discoverable slot.
        retired_generation_path = (
            _prepared_publication_archive_generation_path(
                state_parent, retired_identity
            )
        )
        retired_generation = _read_prepared_publication_archive(
            state_parent=state_parent,
            state_parent_fd=state_parent_fd,
            identity=retired_identity,
            path=retired_generation_path,
            require_full_identity=True,
        )
        if retired_generation is None:
            retired_value = _read_direct_finalization_record(
                _prepared_publication_archive_path(state_parent, identity),
                maximum_bytes=(
                    _ABSOLUTE_MAX_PREPARED_PUBLICATION_ARCHIVE_BYTES
                ),
                directory_fd=state_parent_fd,
            )
            if retired_value is None:
                raise ValueError("prepared publication archive disappeared")
            retired_archive_max = (
                _ABSOLUTE_MAX_PREPARED_PUBLICATION_ARCHIVE_BYTES
                if retired_value.get("schema_version")
                == _PREPARED_PUBLICATION_ARCHIVE_SCHEMA_LEGACY
                else retired_value.get("max_archive_bytes")
            )
            if (
                isinstance(retired_archive_max, bool)
                or not isinstance(retired_archive_max, int)
                or not 0 < retired_archive_max
                <= _ABSOLUTE_MAX_PREPARED_PUBLICATION_ARCHIVE_BYTES
            ):
                raise ValueError("prepared publication archive cap mismatch")
            _write_direct_finalization_record(
                retired_generation_path,
                retired_value,
                maximum_bytes=retired_archive_max,
                directory_fd=state_parent_fd,
            )
        elif retired_generation != existing:
            raise ValueError(
                "prepared publication archive generation changed on replay"
            )
    persisted_receipt_max = _persisted_publication_receipt_max_bytes(receipt)
    if len(receipt_bytes) > persisted_receipt_max:
        raise ValueError("publication receipt exceeds its absolute byte limit")
    value = {
        "schema_version": _PREPARED_PUBLICATION_ARCHIVE_SCHEMA,
        "status": "prepared",
        **dict(identity),
        "receipt_sha256": hashlib.sha256(receipt_bytes).hexdigest(),
        "receipt_bytes": len(receipt_bytes),
        "max_receipt_bytes": persisted_receipt_max,
        "max_archive_bytes": _MAX_PREPARED_PUBLICATION_ARCHIVE_BYTES,
        "receipt": receipt,
        "archived_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    _write_direct_finalization_record(
        _prepared_publication_archive_path(state_parent, identity),
        value,
        maximum_bytes=_MAX_PREPARED_PUBLICATION_ARCHIVE_BYTES,
        replace_existing=existing is not None,
        existing_maximum_bytes=(
            _ABSOLUTE_MAX_PREPARED_PUBLICATION_ARCHIVE_BYTES
        ),
        directory_fd=state_parent_fd,
    )
    return expected


def _prepared_publication_nonpublication_result(
    settlement: Mapping[str, Any]
) -> Dict[str, Any]:
    reason = str(settlement["reason"])
    detail = (
        "The draft or statement changed after verifier evidence was prepared."
        if reason == "prepared_request_drift"
        else "The publication target changed after verifier evidence was prepared."
    )
    return {
        "published": False,
        "verdict": "wrong",
        "verification_status": "final",
        "publication_blocked_reason": reason,
        "prepared_publication_receipt_sha256": settlement["receipt_sha256"],
        "proof_digest": settlement["proof_digest"],
        "context_digest": settlement["context_digest"],
        "repair_hints": (
            detail
            + " The prepared receipt was retained as rejected evidence; a changed "
            "candidate may begin a new explicitly bound verification."
        ),
    }


def _read_prepared_publication_settlement(
    *,
    receipt_path: Path,
    identity: Mapping[str, Any],
    state_parent_fd: int | None = None,
    receipt_parent_fd: int | None = None,
) -> dict[str, Any] | None:
    path = _prepared_publication_settlement_path(
        receipt_path, str(identity["receipt_sha256"])
    )
    primary_fd = (
        state_parent_fd
        if state_parent_fd is not None
        else receipt_parent_fd
    )
    primary_value = _read_direct_finalization_record(
        path,
        maximum_bytes=_MAX_PREPARED_PUBLICATION_SETTLEMENT_BYTES,
        directory_fd=primary_fd,
    )
    receipt_value: dict[str, Any] | None = None
    if (
        receipt_parent_fd is not None
        and receipt_parent_fd != primary_fd
    ):
        receipt_value = _read_direct_finalization_record(
            path,
            maximum_bytes=_MAX_PREPARED_PUBLICATION_SETTLEMENT_BYTES,
            directory_fd=receipt_parent_fd,
        )
        if (
            primary_value is not None
            and receipt_value is not None
            and receipt_value != primary_value
        ):
            raise ValueError("prepared publication settlement mirrors differ")
    value = primary_value if primary_value is not None else receipt_value
    if value is None:
        return None
    expected_keys = {
        "schema_version", "status", "reason", "problem_id",
        "statement_source_digest", "canonical_target_digest", "proof_digest",
        "context_digest", "proof_context_source_sha256", "supersedes",
        "verified_path", "receipt_sha256", "receipt_bytes",
        "persisted_target_precondition", "observed_target_precondition",
        "settled_at_utc",
    }
    if (
        set(value) != expected_keys
        or value.get("schema_version")
        != _PREPARED_PUBLICATION_SETTLEMENT_SCHEMA
        or value.get("status") != "not_published"
        or value.get("reason")
        not in {"prepared_request_drift", "prepared_target_collision"}
        or any(
            value.get(field) != identity.get(field)
            for field in {
                "problem_id", "statement_source_digest",
                "canonical_target_digest", "proof_digest", "context_digest",
                "proof_context_source_sha256", "supersedes",
                "verified_path", "receipt_sha256", "receipt_bytes",
                "persisted_target_precondition",
            }
        )
    ):
        raise ValueError("prepared publication settlement binding mismatch")
    _validate_publication_target_precondition(
        value.get("observed_target_precondition")
    )
    try:
        settled = datetime.fromisoformat(value["settled_at_utc"])
    except (TypeError, ValueError) as exc:
        raise ValueError("prepared publication settlement timestamp is invalid") from exc
    if (
        settled.tzinfo is None
        or settled.utcoffset() != timedelta(0)
        or value["settled_at_utc"]
        != settled.astimezone(timezone.utc).isoformat()
    ):
        raise ValueError("prepared publication settlement timestamp is invalid")
    if (
        receipt_parent_fd is not None
        and receipt_parent_fd != primary_fd
    ):
        # Repair either half of the two-location durable record only after the
        # surviving half has passed its complete identity validation.
        if primary_value is None:
            assert primary_fd is not None
            _write_direct_finalization_record(
                path,
                value,
                maximum_bytes=_MAX_PREPARED_PUBLICATION_SETTLEMENT_BYTES,
                directory_fd=primary_fd,
            )
        if receipt_value is None:
            _write_direct_finalization_record(
                path,
                value,
                maximum_bytes=_MAX_PREPARED_PUBLICATION_SETTLEMENT_BYTES,
                directory_fd=receipt_parent_fd,
            )
    return value


def _commit_prepared_publication_settlement(
    *,
    receipt_path: Path,
    identity: Mapping[str, Any],
    reason: str,
    observed_target_precondition: Mapping[str, Any],
    state_parent_fd: int | None = None,
    receipt_parent_fd: int | None = None,
) -> dict[str, Any]:
    existing = _read_prepared_publication_settlement(
        receipt_path=receipt_path,
        identity=identity,
        state_parent_fd=state_parent_fd,
        receipt_parent_fd=receipt_parent_fd,
    )
    if existing is not None:
        if (
            existing["reason"] != reason
            or existing["observed_target_precondition"]
            != dict(observed_target_precondition)
        ):
            raise ValueError("prepared publication already settled differently")
        return existing
    value = {
        "schema_version": _PREPARED_PUBLICATION_SETTLEMENT_SCHEMA,
        "status": "not_published",
        "reason": reason,
        **dict(identity),
        "observed_target_precondition": dict(observed_target_precondition),
        "settled_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    path = _prepared_publication_settlement_path(
        receipt_path, str(identity["receipt_sha256"])
    )
    primary_fd = (
        state_parent_fd
        if state_parent_fd is not None
        else receipt_parent_fd
    )
    committed = _write_direct_finalization_record(
        path,
        value,
        maximum_bytes=_MAX_PREPARED_PUBLICATION_SETTLEMENT_BYTES,
        directory_fd=primary_fd,
    )
    if (
        receipt_parent_fd is not None
        and receipt_parent_fd != primary_fd
    ):
        _write_direct_finalization_record(
            path,
            committed,
            maximum_bytes=_MAX_PREPARED_PUBLICATION_SETTLEMENT_BYTES,
            directory_fd=receipt_parent_fd,
        )
    return committed


_DIRECT_FINALIZATION_JOURNAL_IDENTITY_FIELDS_V1 = (
    "problem_id",
    "statement_source_digest",
    "canonical_target_digest",
    "proof_digest",
    "context_digest",
    "checked_item_count",
    "checked_item_ids_sha256",
    "draft_path",
    "verified_path",
    "receipt_path",
    "verification_quorum",
    "supersedes",
)
_DIRECT_FINALIZATION_JOURNAL_IDENTITY_FIELDS = (
    *_DIRECT_FINALIZATION_JOURNAL_IDENTITY_FIELDS_V1,
    "endpoint",
    "verification_profile",
    "proof_context_sha256",
    "client_source_sha256",
    "publication_generation_parent_sha256",
)


def _direct_finalization_journal_key(intent_seed: Mapping[str, Any]) -> str:
    identity = {
        "schema_version": "rethlas_direct_finalization_journal_identity_v2",
        **{
            field: intent_seed.get(field)
            for field in _DIRECT_FINALIZATION_JOURNAL_IDENTITY_FIELDS
        },
    }
    return hashlib.sha256(_canonical_json_line_bytes(identity)).hexdigest()


def _direct_finalization_journal_key_v1(
    intent_seed: Mapping[str, Any]
) -> str:
    identity = {
        "schema_version": "rethlas_direct_finalization_journal_identity_v1",
        **{
            field: intent_seed.get(field)
            for field in _DIRECT_FINALIZATION_JOURNAL_IDENTITY_FIELDS_V1
        },
    }
    return hashlib.sha256(_canonical_json_line_bytes(identity)).hexdigest()


def _direct_finalization_paths(
    receipt_path: Path,
    journal_key: str,
    *,
    journal_parent: Path | None = None,
) -> tuple[Path, Path, Path]:
    if _HEX_DIGEST_RE.fullmatch(journal_key) is None:
        raise ValueError("direct finalization journal key is invalid")
    # The semantic key already binds the full receipt path.  Keep the actual
    # directory-entry name fixed-width so a legal long problem component does
    # not turn the journal into an ENAMETOOLONG failure.
    prefix = f".rethlas-verification-{journal_key}"
    parent = receipt_path.parent if journal_parent is None else journal_parent
    return (
        parent / (prefix + ".intent.json"),
        parent / (prefix + ".dispatch.json"),
        parent / (prefix + ".result.json"),
    )


def _direct_finalization_journal_parent(
    *,
    receipt_path: Path,
    verified_path: Path,
    blueprint_root: Path | None,
) -> Path:
    """Choose the stable common ancestor of both publication endpoints."""

    publication_root = (
        _absolute_path(blueprint_root)
        if blueprint_root is not None
        else _absolute_path(verified_path).parent
    )
    receipt_root = _absolute_path(receipt_path).parent
    common = Path(
        os.path.commonpath((str(publication_root), str(receipt_root)))
    )
    if common == Path(common.anchor):
        raise ValueError(
            "publication and receipt trees lack a trusted common journal ancestor"
        )
    # Journal basenames already contain the complete semantic request digest.
    # Keeping them directly in this ancestor avoids a replaceable intermediate
    # directory whose rename could hide a durable dispatch from the next
    # process while leaving both logical endpoint pathnames reusable.
    return common


def _publication_identity_lock_name(*, receipt_path: Path) -> str:
    digest = hashlib.sha256(
        _canonical_json_line_bytes(
            {
                "schema_version": "rethlas_publication_identity_lock_v1",
                "receipt_path": str(_absolute_path(receipt_path)),
            }
        )
    ).hexdigest()
    return f".rethlas-publication-identity-{digest}.lock"


def _legacy_direct_finalization_paths(
    receipt_path: Path, proof_sha256: str
) -> tuple[Path, Path, Path]:
    """Locate journals written before semantic request keys were introduced."""

    prefix = f".{receipt_path.name}.verification-{proof_sha256}"
    return (
        receipt_path.with_name(prefix + ".intent.json"),
        receipt_path.with_name(prefix + ".dispatch.json"),
        receipt_path.with_name(prefix + ".result.json"),
    )


def _legacy_direct_finalization_paths_are_addressable(
    paths: tuple[Path, Path, Path],
) -> bool:
    # Linux/ext4 and the supported test hosts expose NAME_MAX=255.  Avoid even
    # probing an older receipt-derived name beyond that bound: open(2) would
    # fail before the new fixed-width journal could be admitted.
    return all(len(os.fsencode(path.name)) <= 255 for path in paths)


def _direct_legacy_snapshot_path(intent_path: Path) -> Path:
    suffix = ".intent.json"
    if not intent_path.name.endswith(suffix):
        raise ValueError("direct finalization intent path is invalid")
    return intent_path.with_name(
        intent_path.name[: -len(suffix)] + ".legacy-snapshot.json"
    )


def _read_direct_legacy_snapshot(
    *,
    snapshot_path: Path,
    journal_parent_fd: int | None,
    journal_key: str,
    receipt_path: Path,
    proof_sha256: str,
) -> tuple[
    dict[str, Any] | None,
    dict[str, Any] | None,
    dict[str, Any] | None,
] | None:
    value = _read_direct_finalization_record(
        snapshot_path,
        maximum_bytes=(
            _ABSOLUTE_MAX_DIRECT_FINALIZATION_LEGACY_SNAPSHOT_BYTES
        ),
        directory_fd=journal_parent_fd,
    )
    if value is None:
        return None
    expected_keys = {
        "schema_version",
        "status",
        "journal_key",
        "receipt_path",
        "proof_digest",
        "legacy_intent",
        "legacy_dispatch",
        "legacy_result",
        "captured_at_utc",
    }
    records = tuple(
        value.get(field)
        for field in ("legacy_intent", "legacy_dispatch", "legacy_result")
    )
    if (
        set(value) != expected_keys
        or value.get("schema_version")
        != _DIRECT_FINALIZATION_LEGACY_SNAPSHOT_SCHEMA
        or value.get("status") != "captured"
        or value.get("journal_key") != journal_key
        or value.get("receipt_path") != str(receipt_path)
        or value.get("proof_digest") != proof_sha256
        or any(record is not None and not isinstance(record, dict) for record in records)
        or all(record is None for record in records)
        or (records[1] is None and records[2] is not None)
    ):
        raise ValueError("legacy direct finalization snapshot mismatch")
    try:
        captured = datetime.fromisoformat(value["captured_at_utc"])
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "legacy direct finalization snapshot timestamp is invalid"
        ) from exc
    if (
        captured.tzinfo is None
        or captured.utcoffset() != timedelta(0)
        or value["captured_at_utc"]
        != captured.astimezone(timezone.utc).isoformat()
    ):
        raise ValueError(
            "legacy direct finalization snapshot timestamp is invalid"
        )
    return records  # type: ignore[return-value]


def _commit_direct_legacy_snapshot(
    *,
    snapshot_path: Path,
    journal_parent_fd: int | None,
    journal_key: str,
    receipt_path: Path,
    proof_sha256: str,
    legacy_intent: dict[str, Any] | None,
    legacy_dispatch: dict[str, Any] | None,
    legacy_result: dict[str, Any] | None,
) -> tuple[
    dict[str, Any] | None,
    dict[str, Any] | None,
    dict[str, Any] | None,
]:
    existing = _read_direct_legacy_snapshot(
        snapshot_path=snapshot_path,
        journal_parent_fd=journal_parent_fd,
        journal_key=journal_key,
        receipt_path=receipt_path,
        proof_sha256=proof_sha256,
    )
    expected = (legacy_intent, legacy_dispatch, legacy_result)
    if existing is not None:
        if existing != expected:
            raise ValueError("legacy direct finalization snapshot changed")
        return expected
    value = {
        "schema_version": _DIRECT_FINALIZATION_LEGACY_SNAPSHOT_SCHEMA,
        "status": "captured",
        "journal_key": journal_key,
        "receipt_path": str(receipt_path),
        "proof_digest": proof_sha256,
        "legacy_intent": legacy_intent,
        "legacy_dispatch": legacy_dispatch,
        "legacy_result": legacy_result,
        "captured_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    _write_direct_finalization_record(
        snapshot_path,
        value,
        maximum_bytes=(
            _ABSOLUTE_MAX_DIRECT_FINALIZATION_LEGACY_SNAPSHOT_BYTES
        ),
        directory_fd=journal_parent_fd,
    )
    return expected


def _read_direct_finalization_record(
    path: Path,
    *,
    maximum_bytes: int,
    directory_fd: int | None = None,
) -> dict[str, Any] | None:
    if directory_fd is not None:
        return _read_canonical_record_at(
            directory_fd,
            path.name,
            maximum_bytes=maximum_bytes,
            label="direct finalization record",
        )
    observed = _read_canonical_publication_receipt(path)
    if observed is None:
        return None
    value, raw = observed
    if len(raw) > maximum_bytes:
        raise ValueError("direct finalization record exceeds its byte cap")
    return value


def _write_direct_finalization_record(
    path: Path,
    value: dict[str, Any],
    *,
    maximum_bytes: int,
    replace_existing: bool = False,
    existing_maximum_bytes: int | None = None,
    directory_fd: int | None = None,
) -> dict[str, Any]:
    if len(_canonical_json_line_bytes(value)) > maximum_bytes:
        raise ValueError("direct finalization record exceeds its byte cap")
    existing = _read_direct_finalization_record(
        path,
        maximum_bytes=(
            maximum_bytes
            if existing_maximum_bytes is None
            else existing_maximum_bytes
        ),
        directory_fd=directory_fd,
    )
    if existing is not None:
        if existing != value:
            if not replace_existing:
                raise ValueError("direct finalization record changed on replay")
        else:
            return existing
    encoded = _canonical_json_line_bytes(value)
    if directory_fd is not None:
        if replace_existing:
            _atomic_replace_at(directory_fd, path.name, encoded)
        else:
            _write_once_canonical_record_at(
                directory_fd,
                path.name,
                value,
                maximum_bytes=maximum_bytes,
                label="direct finalization record",
            )
        observed = _read_direct_finalization_record(
            path,
            maximum_bytes=maximum_bytes,
            directory_fd=directory_fd,
        )
        if observed != value:
            raise ValueError(
                "direct finalization record changed after publication"
            )
        return observed
    directory_fd = _open_or_create_directory_durable(
        path.parent, label="direct finalization parent"
    )
    try:
        metadata = _lstat_at(directory_fd, path.name)
        if metadata is not None and not replace_existing:
            raise ValueError("direct finalization record collided before write")
        _atomic_replace_at(directory_fd, path.name, encoded)
        _assert_directory_binding(
            path.parent,
            directory_fd,
            label="direct finalization parent",
        )
    finally:
        os.close(directory_fd)
    observed = _read_direct_finalization_record(
        path, maximum_bytes=maximum_bytes
    )
    if observed != value:
        raise ValueError("direct finalization record changed after publication")
    return observed


_PUBLICATION_ADMISSION_IDENTITY_FIELDS = (
    "receipt_path",
    "problem_id",
    "statement_source_digest",
    "canonical_target_digest",
    "proof_digest",
    "context_digest",
    "proof_context_source_sha256",
    "client_source_sha256",
    "supersedes",
    "verified_path",
    "endpoint",
    "verification_profile",
    "verification_quorum",
)
_PUBLICATION_ADMISSION_TARGET_FIELDS = (
    "receipt_path",
    "problem_id",
    "verified_path",
)


def _publication_admission_path(
    state_parent: Path, receipt_path: Path
) -> Path:
    key = hashlib.sha256(
        _canonical_json_line_bytes(
            {
                "schema_version": "rethlas_publication_admission_key_v1",
                "receipt_path": str(_absolute_path(receipt_path)),
            }
        )
    ).hexdigest()
    return state_parent / f".rethlas-publication-admission-{key}.json"


def _publication_admission_generation_path(
    state_parent: Path, admission: Mapping[str, Any]
) -> Path:
    digest = hashlib.sha256(_canonical_json_line_bytes(admission)).hexdigest()
    return state_parent / f".rethlas-publication-admission-generation-{digest}.json"


def _validate_outer_publication_recovery_authority(
    value: object,
    *,
    admission: Mapping[str, Any],
) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {
        "schema_version",
        "intent",
        "dispatch",
        "settlement",
    }:
        raise VerificationExecutionUnknown(
            "outer publication recovery authority is unavailable"
        )
    intent = value.get("intent")
    dispatch = value.get("dispatch")
    settlement = value.get("settlement")
    if not all(isinstance(record, Mapping) for record in (intent, dispatch, settlement)):
        raise VerificationExecutionUnknown(
            "outer publication recovery authority is unavailable"
        )
    assert isinstance(intent, Mapping)
    assert isinstance(dispatch, Mapping)
    assert isinstance(settlement, Mapping)
    intent_sha256 = hashlib.sha256(
        _canonical_json_line_bytes(dict(intent))
    ).hexdigest()
    dispatch_sha256 = hashlib.sha256(
        _canonical_json_line_bytes(dict(dispatch))
    ).hexdigest()
    settlement_sha256 = hashlib.sha256(
        _canonical_json_line_bytes(dict(settlement))
    ).hexdigest()
    if (
        value.get("schema_version")
        != _OUTER_PUBLICATION_RECOVERY_AUTHORITY_SCHEMA
        or intent.get("status") != "submitted"
        or intent.get("problem_id") != admission.get("problem_id")
        or intent.get("statement_sha256")
        != admission.get("statement_source_digest")
        or intent.get("blueprint_sha256") != admission.get("proof_digest")
        or dispatch.get("status") != "dispatched"
        or dispatch.get("problem_id") != intent.get("problem_id")
        or dispatch.get("statement_sha256") != intent.get("statement_sha256")
        or dispatch.get("blueprint_sha256") != intent.get("blueprint_sha256")
        or dispatch.get("intent_sha256") != intent_sha256
        or settlement.get("status") != "not_published"
        or settlement.get("problem_id") != intent.get("problem_id")
        or settlement.get("statement_sha256") != intent.get("statement_sha256")
        or settlement.get("blueprint_sha256") != intent.get("blueprint_sha256")
        or settlement.get("intent_sha256") != intent_sha256
        or settlement.get("publication_receipt_sha256") is not None
        or (
            admission.get("external_authority_intent_sha256") is not None
            and admission.get("external_authority_intent_sha256")
            != intent_sha256
        )
    ):
        raise VerificationExecutionUnknown(
            "outer publication recovery authority binding mismatch"
        )
    return {
        "schema_version": _OUTER_PUBLICATION_RECOVERY_AUTHORITY_SCHEMA,
        "intent": dict(intent),
        "intent_sha256": intent_sha256,
        "dispatch": dict(dispatch),
        "dispatch_sha256": dispatch_sha256,
        "settlement": dict(settlement),
        "settlement_sha256": settlement_sha256,
    }


def _publication_recovery_certificate_path(
    state_parent: Path, certificate_sha256: str
) -> Path:
    if _HEX_DIGEST_RE.fullmatch(certificate_sha256) is None:
        raise ValueError("publication recovery certificate digest is invalid")
    return state_parent / (
        f".rethlas-publication-recovery-{certificate_sha256}.json"
    )


def _write_publication_recovery_certificate(
    *,
    state_parent: Path,
    state_parent_fd: int,
    seed: Mapping[str, Any],
) -> tuple[dict[str, Any], str]:
    certificate_sha256 = hashlib.sha256(
        _canonical_json_line_bytes(dict(seed))
    ).hexdigest()
    certificate = {
        **dict(seed),
        "certificate_sha256": certificate_sha256,
    }
    _write_direct_finalization_record(
        _publication_recovery_certificate_path(
            state_parent, certificate_sha256
        ),
        certificate,
        maximum_bytes=_MAX_PUBLICATION_RECOVERY_CERTIFICATE_BYTES,
        directory_fd=state_parent_fd,
    )
    return certificate, certificate_sha256


def _publication_admission_identity(
    *,
    receipt_path: Path,
    archive_identity: Mapping[str, Any],
    client_source_sha256: str,
    endpoint: str,
    verification_profile: str,
    verification_quorum: int,
) -> dict[str, Any]:
    return {
        "receipt_path": str(_absolute_path(receipt_path)),
        **{
            field: archive_identity[field]
            for field in _PREPARED_PUBLICATION_ARCHIVE_IDENTITY_FIELDS
        },
        "client_source_sha256": client_source_sha256,
        "endpoint": endpoint,
        "verification_profile": verification_profile,
        "verification_quorum": verification_quorum,
    }


def _read_publication_admission(
    *,
    state_parent: Path,
    state_parent_fd: int,
    receipt_path: Path,
) -> dict[str, Any] | None:
    value = _read_direct_finalization_record(
        _publication_admission_path(state_parent, receipt_path),
        maximum_bytes=_MAX_PUBLICATION_ADMISSION_BYTES,
        directory_fd=state_parent_fd,
    )
    if value is None:
        return None
    common_keys = {
        "schema_version",
        "status",
        *_PUBLICATION_ADMISSION_IDENTITY_FIELDS,
        "generation_parent_sha256",
        "created_at_utc",
        "settlement_receipt_sha256",
        "settlement_reason",
        "settled_at_utc",
    }
    schema = value.get("schema_version")
    legacy = schema == _PUBLICATION_ADMISSION_SCHEMA_LEGACY
    previous = schema == _PUBLICATION_ADMISSION_SCHEMA_PREVIOUS
    verifier_identity_schema = (
        schema == _PUBLICATION_ADMISSION_SCHEMA_VERIFIER_IDENTITY
    )
    expected_keys = common_keys if legacy else common_keys | {
        "phase",
        "effect_intent_sha256",
        "effect_dispatch_name",
    }
    if verifier_identity_schema or schema == _PUBLICATION_ADMISSION_SCHEMA:
        expected_keys.add("verifier_effect_identity_sha256")
    if schema == _PUBLICATION_ADMISSION_SCHEMA:
        expected_keys.update(
            {
                "external_authority_intent_sha256",
                "settlement_evidence_sha256",
            }
        )
    digest_fields = {
        "statement_source_digest",
        "canonical_target_digest",
        "proof_digest",
        "context_digest",
        "proof_context_source_sha256",
        "client_source_sha256",
    }
    if (
        set(value) != expected_keys
        or schema not in {
            _PUBLICATION_ADMISSION_SCHEMA_LEGACY,
            _PUBLICATION_ADMISSION_SCHEMA_PREVIOUS,
            _PUBLICATION_ADMISSION_SCHEMA_VERIFIER_IDENTITY,
            _PUBLICATION_ADMISSION_SCHEMA,
        }
        or value.get("status") not in {"submitted", "settled"}
        or value.get("receipt_path") != str(_absolute_path(receipt_path))
        or any(
            not isinstance(value.get(field), str)
            or _HEX_DIGEST_RE.fullmatch(value[field]) is None
            for field in digest_fields
        )
        or not isinstance(value.get("problem_id"), str)
        or not value["problem_id"]
        or not isinstance(value.get("verified_path"), str)
        or not Path(value["verified_path"]).is_absolute()
        or not isinstance(value.get("endpoint"), str)
        or value.get("verification_profile")
        not in {"compatible", "balanced", "economy", "max_diversity"}
        or value.get("verification_quorum") != 2
        or isinstance(value.get("verification_quorum"), bool)
        or not isinstance(value.get("supersedes"), list)
        or len(value["supersedes"]) > 1
        or (
            value.get("generation_parent_sha256") is not None
            and (
                not isinstance(value["generation_parent_sha256"], str)
                or _HEX_DIGEST_RE.fullmatch(
                    value["generation_parent_sha256"]
                )
                is None
            )
        )
    ):
        raise ValueError("publication admission binding mismatch")
    if legacy:
        # A historical submitted admission did not distinguish pre-dispatch
        # from post-dispatch.  Treat it conservatively as dispatched; a
        # terminal local result can still settle it below.
        value = {
            **value,
            "schema_version": _PUBLICATION_ADMISSION_SCHEMA,
            "phase": (
                "legacy_unknown"
                if value["status"] == "submitted"
                else "settled"
            ),
            "effect_intent_sha256": None,
            "effect_dispatch_name": None,
            "verifier_effect_identity_sha256": None,
            "external_authority_intent_sha256": None,
            "settlement_evidence_sha256": None,
        }
    elif previous:
        value = {
            **value,
            "schema_version": _PUBLICATION_ADMISSION_SCHEMA,
            "verifier_effect_identity_sha256": None,
            "external_authority_intent_sha256": None,
            "settlement_evidence_sha256": None,
        }
    elif verifier_identity_schema:
        value = {
            **value,
            "schema_version": _PUBLICATION_ADMISSION_SCHEMA,
            "external_authority_intent_sha256": None,
            "settlement_evidence_sha256": None,
        }
    if (
        value.get("phase")
        not in {"reserved", "dispatched", "legacy_unknown", "settled"}
        or (
            value.get("effect_intent_sha256") is not None
            and (
                not isinstance(value["effect_intent_sha256"], str)
                or _HEX_DIGEST_RE.fullmatch(
                    value["effect_intent_sha256"]
                )
                is None
            )
        )
        or (
            value.get("effect_dispatch_name") is not None
            and (
                not isinstance(value["effect_dispatch_name"], str)
                or Path(value["effect_dispatch_name"]).name
                != value["effect_dispatch_name"]
                or not value["effect_dispatch_name"].startswith(
                    ".rethlas-verification-"
                )
                or not value["effect_dispatch_name"].endswith(
                    ".dispatch.json"
                )
            )
        )
        or (
            value.get("verifier_effect_identity_sha256") is not None
            and (
                not isinstance(
                    value["verifier_effect_identity_sha256"], str
                )
                or _HEX_DIGEST_RE.fullmatch(
                    value["verifier_effect_identity_sha256"]
                )
                is None
            )
        )
        or any(
            value.get(field) is not None
            and (
                not isinstance(value[field], str)
                or _HEX_DIGEST_RE.fullmatch(value[field]) is None
            )
            for field in (
                "external_authority_intent_sha256",
                "settlement_evidence_sha256",
            )
        )
        or (
            value["status"] == "submitted"
            and value["phase"]
            not in {"reserved", "dispatched", "legacy_unknown"}
        )
        or (
            value["status"] == "settled"
            and value["phase"] != "settled"
        )
    ):
        raise ValueError("publication admission phase mismatch")
    for field in ("created_at_utc",):
        try:
            timestamp = datetime.fromisoformat(value[field])
        except (TypeError, ValueError) as exc:
            raise ValueError("publication admission timestamp mismatch") from exc
        if (
            timestamp.tzinfo is None
            or timestamp.utcoffset() != timedelta(0)
            or value[field]
            != timestamp.astimezone(timezone.utc).isoformat()
        ):
            raise ValueError("publication admission timestamp mismatch")
    if value["status"] == "submitted":
        if any(
            value.get(field) is not None
            for field in (
                "settlement_receipt_sha256",
                "settlement_reason",
                "settled_at_utc",
                "settlement_evidence_sha256",
            )
        ):
            raise ValueError("publication admission settlement mismatch")
    else:
        if (
            value.get("settlement_reason")
            not in {
                "prepared_request_drift",
                "prepared_target_collision",
                "direct_nonpublication",
                "direct_mathematical_rejection",
                "direct_operational_nonpublication",
                "external_operational_nonpublication",
                "predispatch_abandoned",
            }
            or (
                value.get("settlement_receipt_sha256") is not None
                and (
                    not isinstance(value["settlement_receipt_sha256"], str)
                    or _HEX_DIGEST_RE.fullmatch(
                        value["settlement_receipt_sha256"]
                    )
                    is None
                )
            )
            or (
                value.get("settlement_reason")
                == "external_operational_nonpublication"
            )
            != (value.get("settlement_evidence_sha256") is not None)
        ):
            raise ValueError("publication admission settlement mismatch")
        try:
            settled = datetime.fromisoformat(value["settled_at_utc"])
        except (TypeError, ValueError) as exc:
            raise ValueError("publication admission timestamp mismatch") from exc
        if (
            settled.tzinfo is None
            or settled.utcoffset() != timedelta(0)
            or value["settled_at_utc"]
            != settled.astimezone(timezone.utc).isoformat()
        ):
            raise ValueError("publication admission timestamp mismatch")
    return value


def _begin_publication_admission(
    *,
    state_parent: Path,
    state_parent_fd: int,
    receipt_path: Path,
    identity: Mapping[str, Any],
    permit_exact_successor: bool,
    external_authority_intent_sha256: str | None,
) -> dict[str, Any]:
    if (
        external_authority_intent_sha256 is not None
        and _HEX_DIGEST_RE.fullmatch(external_authority_intent_sha256) is None
    ):
        raise ValueError("external publication authority intent is invalid")
    existing = _read_publication_admission(
        state_parent=state_parent,
        state_parent_fd=state_parent_fd,
        receipt_path=receipt_path,
    )
    identity_matches = existing is not None and all(
        existing.get(field) == identity.get(field)
        for field in _PUBLICATION_ADMISSION_IDENTITY_FIELDS
    )
    if (
        identity_matches
        and existing["status"] == "submitted"
        and existing.get("external_authority_intent_sha256")
        == external_authority_intent_sha256
    ):
        return existing
    if (
        identity_matches
        and existing is not None
        and not permit_exact_successor
    ):
        # Direct finalization owns a durable negative-result journal.  Return
        # the settled slot so its caller can replay that result without a
        # profile request or a second verifier effect.
        return existing
    if (
        identity_matches
        and existing is not None
        and existing["settlement_reason"]
        not in _RETRYABLE_PUBLICATION_ADMISSION_SETTLEMENTS
    ):
        raise ValueError(
            "this exact proof already has a settled non-retryable verification"
        )
    generation_parent_sha256 = None
    if existing is not None:
        if existing["status"] != "settled":
            retirement_reason = "predispatch_abandoned"
            if existing["phase"] == "legacy_unknown":
                raise VerificationExecutionUnknown(
                    "legacy publication admission has no effect-phase binding"
                )
            if existing["phase"] == "dispatched":
                dispatch_name = existing.get("effect_dispatch_name")
                if dispatch_name is None:
                    raise VerificationExecutionUnknown(
                        "canonical receipt has an unresolved publication admission"
                    )
                dispatch_path = state_parent / dispatch_name
                dispatch = _read_direct_finalization_record(
                    dispatch_path,
                    maximum_bytes=(
                        _ABSOLUTE_MAX_DIRECT_FINALIZATION_INTENT_BYTES
                    ),
                    directory_fd=state_parent_fd,
                )
                if dispatch is not None:
                    intent_sha256 = existing.get("effect_intent_sha256")
                    if (
                        dispatch.get("schema_version")
                        != _DIRECT_FINALIZATION_DISPATCH_SCHEMA
                        or dispatch.get("status") != "dispatched"
                        or dispatch.get("intent_sha256") != intent_sha256
                    ):
                        raise ValueError(
                            "publication admission dispatch binding mismatch"
                        )
                    result_name = dispatch_name.removesuffix(
                        ".dispatch.json"
                    ) + ".result.json"
                    result = _read_direct_finalization_record(
                        state_parent / result_name,
                        maximum_bytes=(
                            _ABSOLUTE_MAX_DIRECT_FINALIZATION_RESULT_BYTES
                        ),
                        directory_fd=state_parent_fd,
                    )
                    dispatch_sha256 = hashlib.sha256(
                        _canonical_json_line_bytes(dispatch)
                    ).hexdigest()
                    if (
                        result is None
                        or result.get("status") != "completed"
                        or result.get("intent_sha256") != intent_sha256
                        or result.get("dispatch_sha256") != dispatch_sha256
                        or not isinstance(result.get("result"), dict)
                        or result["result"].get("published") is not False
                    ):
                        raise VerificationExecutionUnknown(
                            "canonical receipt has an unresolved publication admission"
                        )
                    retirement_reason = "direct_nonpublication"
            # Nothing can reach GET/POST until the admission is durably moved
            # out of reserved.  A different request can therefore retire a
            # crash-left pre-dispatch reservation without risking a duplicate
            # verifier effect.
            existing = {
                **existing,
                "status": "settled",
                "phase": "settled",
                "settlement_receipt_sha256": None,
                "settlement_reason": retirement_reason,
                "settled_at_utc": datetime.now(timezone.utc).isoformat(),
            }
        generation_parent_sha256 = hashlib.sha256(
            _canonical_json_line_bytes(existing)
        ).hexdigest()
        # Publish the immutable retired generation first, then replace the
        # discovery slot directly with its successor.  A crash before the
        # replacement leaves the original replayable slot; a crash after it
        # leaves the successor.  No half-migrated settled slot is exposed.
        _write_direct_finalization_record(
            _publication_admission_generation_path(
                state_parent, existing
            ),
            existing,
            maximum_bytes=_MAX_PUBLICATION_ADMISSION_BYTES,
            directory_fd=state_parent_fd,
        )
    value = {
        "schema_version": _PUBLICATION_ADMISSION_SCHEMA,
        "status": "submitted",
        "phase": "reserved",
        "effect_intent_sha256": None,
        "effect_dispatch_name": None,
        "verifier_effect_identity_sha256": None,
        "external_authority_intent_sha256": (
            external_authority_intent_sha256
        ),
        "settlement_evidence_sha256": None,
        **{
            field: identity[field]
            for field in _PUBLICATION_ADMISSION_IDENTITY_FIELDS
        },
        "generation_parent_sha256": generation_parent_sha256,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "settlement_receipt_sha256": None,
        "settlement_reason": None,
        "settled_at_utc": None,
    }
    _write_direct_finalization_record(
        _publication_admission_path(state_parent, receipt_path),
        value,
        maximum_bytes=_MAX_PUBLICATION_ADMISSION_BYTES,
        replace_existing=existing is not None,
        directory_fd=state_parent_fd,
    )
    return value


def _bind_publication_admission_effect_intent(
    *,
    state_parent: Path,
    state_parent_fd: int,
    receipt_path: Path,
    effect_intent_sha256: str,
    effect_dispatch_name: str | None = None,
) -> dict[str, Any]:
    if _HEX_DIGEST_RE.fullmatch(effect_intent_sha256) is None:
        raise ValueError("publication admission effect intent is invalid")
    if effect_dispatch_name is not None and (
        Path(effect_dispatch_name).name != effect_dispatch_name
        or not effect_dispatch_name.startswith(
            ".rethlas-verification-"
        )
        or not effect_dispatch_name.endswith(".dispatch.json")
    ):
        raise ValueError("publication admission dispatch name is invalid")
    existing = _read_publication_admission(
        state_parent=state_parent,
        state_parent_fd=state_parent_fd,
        receipt_path=receipt_path,
    )
    if existing is None or existing["status"] != "submitted":
        raise ValueError("publication admission is not submitted")
    if existing["phase"] == "legacy_unknown":
        if effect_dispatch_name is None:
            raise VerificationExecutionUnknown(
                "legacy publication admission lacks a local dispatch binding"
            )
        dispatch = _read_direct_finalization_record(
            state_parent / effect_dispatch_name,
            maximum_bytes=_ABSOLUTE_MAX_DIRECT_FINALIZATION_INTENT_BYTES,
            directory_fd=state_parent_fd,
        )
        if dispatch is not None:
            raise VerificationExecutionUnknown(
                "legacy publication admission crossed its dispatch fence"
            )
        value = {
            **existing,
            "phase": "reserved",
            "effect_intent_sha256": effect_intent_sha256,
            "effect_dispatch_name": effect_dispatch_name,
        }
        _write_direct_finalization_record(
            _publication_admission_path(state_parent, receipt_path),
            value,
            maximum_bytes=_MAX_PUBLICATION_ADMISSION_BYTES,
            replace_existing=True,
            directory_fd=state_parent_fd,
        )
        return value
    prior = existing.get("effect_intent_sha256")
    if prior is not None:
        if (
            prior != effect_intent_sha256
            or existing.get("effect_dispatch_name")
            != effect_dispatch_name
        ):
            if existing["phase"] == "reserved":
                value = {
                    **existing,
                    "effect_intent_sha256": effect_intent_sha256,
                    "effect_dispatch_name": effect_dispatch_name,
                }
                _write_direct_finalization_record(
                    _publication_admission_path(state_parent, receipt_path),
                    value,
                    maximum_bytes=_MAX_PUBLICATION_ADMISSION_BYTES,
                    replace_existing=True,
                    directory_fd=state_parent_fd,
                )
                return value
            raise ValueError("publication admission effect intent changed")
        return existing
    if existing["phase"] != "reserved":
        raise ValueError("publication admission effect intent is missing")
    value = {
        **existing,
        "effect_intent_sha256": effect_intent_sha256,
        "effect_dispatch_name": effect_dispatch_name,
    }
    _write_direct_finalization_record(
        _publication_admission_path(state_parent, receipt_path),
        value,
        maximum_bytes=_MAX_PUBLICATION_ADMISSION_BYTES,
        replace_existing=True,
        directory_fd=state_parent_fd,
    )
    return value


def _bind_publication_admission_verifier_effect_identity(
    *,
    state_parent: Path,
    state_parent_fd: int,
    receipt_path: Path,
    verifier_effect_identity_sha256: str,
) -> dict[str, Any]:
    if _HEX_DIGEST_RE.fullmatch(verifier_effect_identity_sha256) is None:
        raise ValueError("publication verifier effect identity is invalid")
    existing = _read_publication_admission(
        state_parent=state_parent,
        state_parent_fd=state_parent_fd,
        receipt_path=receipt_path,
    )
    if existing is None or existing["status"] != "submitted":
        raise ValueError("publication admission is not submitted")
    prior = existing.get("verifier_effect_identity_sha256")
    if prior == verifier_effect_identity_sha256:
        return existing
    if prior is not None and existing["phase"] != "reserved":
        raise VerificationExecutionUnknown(
            "dispatched publication admission changed verifier identity"
        )
    if prior is None and existing["phase"] in {
        "dispatched",
        "legacy_unknown",
    }:
        raise VerificationExecutionUnknown(
            "dispatched publication admission lacks verifier identity"
        )
    value = {
        **existing,
        "schema_version": _PUBLICATION_ADMISSION_SCHEMA,
        "verifier_effect_identity_sha256": (
            verifier_effect_identity_sha256
        ),
    }
    _write_direct_finalization_record(
        _publication_admission_path(state_parent, receipt_path),
        value,
        maximum_bytes=_MAX_PUBLICATION_ADMISSION_BYTES,
        replace_existing=True,
        directory_fd=state_parent_fd,
    )
    return value


def _mark_publication_admission_dispatched(
    *,
    state_parent: Path,
    state_parent_fd: int,
    receipt_path: Path,
    effect_intent_sha256: str,
    effect_dispatch_name: str | None = None,
) -> dict[str, Any]:
    existing = _bind_publication_admission_effect_intent(
        state_parent=state_parent,
        state_parent_fd=state_parent_fd,
        receipt_path=receipt_path,
        effect_intent_sha256=effect_intent_sha256,
        effect_dispatch_name=effect_dispatch_name,
    )
    if existing["phase"] == "dispatched":
        return existing
    if existing["phase"] != "reserved":
        raise ValueError("publication admission phase changed before dispatch")
    if existing.get("verifier_effect_identity_sha256") is None:
        raise ValueError(
            "publication admission lacks a bound verifier identity"
        )
    value = {**existing, "phase": "dispatched"}
    _write_direct_finalization_record(
        _publication_admission_path(state_parent, receipt_path),
        value,
        maximum_bytes=_MAX_PUBLICATION_ADMISSION_BYTES,
        replace_existing=True,
        directory_fd=state_parent_fd,
    )
    return value


def _reset_publication_admission_after_failed_dispatch(
    *,
    state_parent: Path,
    state_parent_fd: int,
    receipt_path: Path,
    effect_intent_sha256: str,
) -> dict[str, Any]:
    existing = _read_publication_admission(
        state_parent=state_parent,
        state_parent_fd=state_parent_fd,
        receipt_path=receipt_path,
    )
    if (
        existing is None
        or existing["status"] != "submitted"
        or existing.get("effect_intent_sha256") != effect_intent_sha256
    ):
        raise ValueError("publication admission changed after failed dispatch")
    if existing["phase"] == "reserved":
        return existing
    if existing["phase"] != "dispatched":
        raise ValueError("publication admission phase changed after dispatch")
    dispatch_name = existing.get("effect_dispatch_name")
    if dispatch_name is not None and _read_direct_finalization_record(
        state_parent / dispatch_name,
        maximum_bytes=_ABSOLUTE_MAX_DIRECT_FINALIZATION_INTENT_BYTES,
        directory_fd=state_parent_fd,
    ) is not None:
        raise VerificationExecutionUnknown(
            "failed verifier dispatch crossed its durable effect fence"
        )
    value = {
        **existing,
        "phase": "reserved",
        "effect_intent_sha256": None,
        "effect_dispatch_name": None,
    }
    _write_direct_finalization_record(
        _publication_admission_path(state_parent, receipt_path),
        value,
        maximum_bytes=_MAX_PUBLICATION_ADMISSION_BYTES,
        replace_existing=True,
        directory_fd=state_parent_fd,
    )
    return value


def _settle_publication_admission(
    *,
    state_parent: Path,
    state_parent_fd: int,
    receipt_path: Path,
    reason: str,
    receipt_sha256: str | None,
    expected_admission_sha256: str | None = None,
    settlement_evidence_sha256: str | None = None,
) -> dict[str, Any]:
    for label, digest in (
        ("expected publication admission", expected_admission_sha256),
        ("publication settlement evidence", settlement_evidence_sha256),
    ):
        if digest is not None and _HEX_DIGEST_RE.fullmatch(digest) is None:
            raise ValueError(f"{label} digest is invalid")
    raw_existing = _read_direct_finalization_record(
        _publication_admission_path(state_parent, receipt_path),
        maximum_bytes=_MAX_PUBLICATION_ADMISSION_BYTES,
        directory_fd=state_parent_fd,
    )
    existing = _read_publication_admission(
        state_parent=state_parent,
        state_parent_fd=state_parent_fd,
        receipt_path=receipt_path,
    )
    if existing is None:
        raise ValueError("prepared settlement lacks publication admission")
    assert raw_existing is not None
    existing_sha256 = hashlib.sha256(
        _canonical_json_line_bytes(raw_existing)
    ).hexdigest()
    if (
        expected_admission_sha256 is not None
        and existing_sha256 != expected_admission_sha256
    ):
        raise VerificationExecutionUnknown(
            "publication admission changed before recovery settlement"
        )
    if existing["status"] == "settled":
        if (
            existing["settlement_reason"] != reason
            or existing["settlement_receipt_sha256"] != receipt_sha256
            or existing.get("settlement_evidence_sha256")
            != settlement_evidence_sha256
        ):
            raise ValueError("publication admission settled differently")
        return existing
    value = {
        **existing,
        "schema_version": _PUBLICATION_ADMISSION_SCHEMA,
        "status": "settled",
        "phase": "settled",
        "settlement_receipt_sha256": receipt_sha256,
        "settlement_reason": reason,
        "settlement_evidence_sha256": settlement_evidence_sha256,
        "settled_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    _write_direct_finalization_record(
        _publication_admission_path(state_parent, receipt_path),
        value,
        maximum_bytes=_MAX_PUBLICATION_ADMISSION_BYTES,
        replace_existing=True,
        directory_fd=state_parent_fd,
    )
    return value


def _receipt_collision_rollback_path(
    state_parent: Path, receipt_path: Path
) -> Path:
    key = hashlib.sha256(
        _canonical_json_line_bytes(
            {
                "schema_version": (
                    "rethlas_receipt_collision_rollback_key_v1"
                ),
                "receipt_path": str(_absolute_path(receipt_path)),
            }
        )
    ).hexdigest()
    return state_parent / f".rethlas-receipt-collision-rollback-{key}.json"


def _receipt_collision_rollback_generation_path(
    state_parent: Path, rollback: Mapping[str, Any]
) -> Path:
    digest = hashlib.sha256(_canonical_json_line_bytes(rollback)).hexdigest()
    return state_parent / f".rethlas-receipt-collision-rollback-generation-{digest}.json"


def _read_receipt_collision_rollback(
    *,
    state_parent: Path,
    state_parent_fd: int,
    receipt_path: Path,
) -> dict[str, Any] | None:
    value = _read_direct_finalization_record(
        _receipt_collision_rollback_path(state_parent, receipt_path),
        maximum_bytes=_MAX_RECEIPT_COLLISION_ROLLBACK_BYTES,
        directory_fd=state_parent_fd,
    )
    if value is None:
        return None
    expected_keys = {
        "schema_version",
        "status",
        "receipt_path",
        *_PREPARED_PUBLICATION_ARCHIVE_IDENTITY_FIELDS,
        "receipt_sha256",
        "expected_target_precondition",
        "published_st_dev",
        "published_st_ino",
        "candidate_name",
        "competing_receipt_sha256",
        "prepared_at_utc",
        "settlement_receipt_sha256",
        "settlement_reason",
        "settled_at_utc",
    }
    if (
        set(value) != expected_keys
        or value.get("schema_version")
        != _RECEIPT_COLLISION_ROLLBACK_SCHEMA
        or value.get("status") not in {"rollback_required", "settled"}
        or value.get("receipt_path") != str(_absolute_path(receipt_path))
        or any(
            not isinstance(value.get(field), str)
            or _HEX_DIGEST_RE.fullmatch(value[field]) is None
            for field in {
                "statement_source_digest",
                "canonical_target_digest",
                "proof_digest",
                "context_digest",
                "proof_context_source_sha256",
                "receipt_sha256",
                "competing_receipt_sha256",
            }
        )
        or not isinstance(value.get("problem_id"), str)
        or not value["problem_id"]
        or not isinstance(value.get("verified_path"), str)
        or not Path(value["verified_path"]).is_absolute()
        or not isinstance(value.get("supersedes"), list)
        or len(value["supersedes"]) > 1
        or isinstance(value.get("published_st_dev"), bool)
        or not isinstance(value.get("published_st_dev"), int)
        or value["published_st_dev"] < 0
        or isinstance(value.get("published_st_ino"), bool)
        or not isinstance(value.get("published_st_ino"), int)
        or value["published_st_ino"] <= 0
        or not isinstance(value.get("candidate_name"), str)
        or not value["candidate_name"]
        or Path(value["candidate_name"]).name != value["candidate_name"]
    ):
        raise ValueError("receipt collision rollback binding mismatch")
    _validate_publication_target_precondition(
        value.get("expected_target_precondition")
    )
    try:
        prepared = datetime.fromisoformat(value["prepared_at_utc"])
    except (TypeError, ValueError) as exc:
        raise ValueError("receipt collision rollback timestamp mismatch") from exc
    if (
        prepared.tzinfo is None
        or prepared.utcoffset() != timedelta(0)
        or value["prepared_at_utc"]
        != prepared.astimezone(timezone.utc).isoformat()
    ):
        raise ValueError("receipt collision rollback timestamp mismatch")
    if value["status"] == "rollback_required":
        if any(
            value.get(field) is not None
            for field in (
                "settlement_receipt_sha256",
                "settlement_reason",
                "settled_at_utc",
            )
        ):
            raise ValueError("receipt collision rollback settlement mismatch")
    else:
        if (
            value.get("settlement_receipt_sha256")
            != value.get("receipt_sha256")
            or value.get("settlement_reason")
            != "prepared_target_collision"
        ):
            raise ValueError("receipt collision rollback settlement mismatch")
        try:
            settled = datetime.fromisoformat(value["settled_at_utc"])
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "receipt collision rollback settlement timestamp mismatch"
            ) from exc
        if (
            settled.tzinfo is None
            or settled.utcoffset() != timedelta(0)
            or value["settled_at_utc"]
            != settled.astimezone(timezone.utc).isoformat()
        ):
            raise ValueError(
                "receipt collision rollback settlement timestamp mismatch"
            )
    return value


def _commit_receipt_collision_rollback(
    *,
    state_parent: Path,
    state_parent_fd: int,
    receipt_path: Path,
    archive_identity: Mapping[str, Any],
    receipt_sha256: str,
    competing_receipt_sha256: str,
    expected_target_precondition: Mapping[str, Any],
    published_identity: tuple[int, int],
    candidate_name: str,
) -> dict[str, Any]:
    value = {
        "schema_version": _RECEIPT_COLLISION_ROLLBACK_SCHEMA,
        "status": "rollback_required",
        "receipt_path": str(_absolute_path(receipt_path)),
        **{
            field: archive_identity[field]
            for field in _PREPARED_PUBLICATION_ARCHIVE_IDENTITY_FIELDS
        },
        "receipt_sha256": receipt_sha256,
        "expected_target_precondition": dict(expected_target_precondition),
        "published_st_dev": published_identity[0],
        "published_st_ino": published_identity[1],
        "candidate_name": candidate_name,
        "competing_receipt_sha256": competing_receipt_sha256,
        "prepared_at_utc": datetime.now(timezone.utc).isoformat(),
        "settlement_receipt_sha256": None,
        "settlement_reason": None,
        "settled_at_utc": None,
    }
    existing = _read_receipt_collision_rollback(
        state_parent=state_parent,
        state_parent_fd=state_parent_fd,
        receipt_path=receipt_path,
    )
    if existing is not None:
        comparable = set(value) - {
            "prepared_at_utc",
            "settlement_receipt_sha256",
            "settlement_reason",
            "settled_at_utc",
            "status",
        }
        if existing["status"] == "rollback_required":
            if any(
                existing.get(field) != value.get(field)
                for field in comparable
            ):
                raise ValueError("receipt collision rollback changed on replay")
            return existing
        _write_direct_finalization_record(
            _receipt_collision_rollback_generation_path(
                state_parent, existing
            ),
            existing,
            maximum_bytes=_MAX_RECEIPT_COLLISION_ROLLBACK_BYTES,
            directory_fd=state_parent_fd,
        )
    _write_direct_finalization_record(
        _receipt_collision_rollback_path(state_parent, receipt_path),
        value,
        maximum_bytes=_MAX_RECEIPT_COLLISION_ROLLBACK_BYTES,
        replace_existing=existing is not None,
        directory_fd=state_parent_fd,
    )
    return value


def _settle_receipt_collision_rollback(
    *,
    state_parent: Path,
    state_parent_fd: int,
    receipt_path: Path,
    settlement: Mapping[str, Any],
) -> dict[str, Any] | None:
    existing = _read_receipt_collision_rollback(
        state_parent=state_parent,
        state_parent_fd=state_parent_fd,
        receipt_path=receipt_path,
    )
    if existing is None:
        return None
    if settlement.get("receipt_sha256") != existing["receipt_sha256"]:
        return None
    if existing["status"] == "settled":
        return existing
    value = {
        **existing,
        "status": "settled",
        "settlement_receipt_sha256": settlement["receipt_sha256"],
        "settlement_reason": settlement["reason"],
        "settled_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    _write_direct_finalization_record(
        _receipt_collision_rollback_path(state_parent, receipt_path),
        value,
        maximum_bytes=_MAX_RECEIPT_COLLISION_ROLLBACK_BYTES,
        replace_existing=True,
        directory_fd=state_parent_fd,
    )
    return value


def _recover_receipt_collision_rollback_at(
    directory_fd: int,
    filename: str,
    *,
    rollback: Mapping[str, Any],
    display_path: Path,
) -> Mapping[str, Any]:
    expected = _validate_publication_target_precondition(
        rollback["expected_target_precondition"]
    )
    published_identity = (
        int(rollback["published_st_dev"]),
        int(rollback["published_st_ino"]),
    )
    candidate_name = str(rollback["candidate_name"])
    target_metadata = _lstat_at(directory_fd, filename)
    target_is_ours = target_metadata is not None and (
        target_metadata.st_dev,
        target_metadata.st_ino,
    ) == published_identity
    if target_is_ours:
        if expected["kind"] == "absent":
            _unlink_if_identity_at(directory_fd, filename, published_identity)
            return _publication_target_precondition_at(
                directory_fd, filename, display_path=display_path
            )
        displaced = _publication_target_precondition_at(
            directory_fd,
            candidate_name,
            display_path=display_path.with_name(candidate_name),
        )
        if displaced != expected:
            raise ValueError("receipt collision retained target changed")
        _renameat2_at(
            directory_fd, candidate_name, filename, _RENAME_EXCHANGE
        )
        os.fsync(directory_fd)
        captured = _lstat_at(directory_fd, candidate_name)
        if captured is None or (
            captured.st_dev,
            captured.st_ino,
        ) != published_identity:
            _renameat2_at(
                directory_fd, candidate_name, filename, _RENAME_EXCHANGE
            )
            os.fsync(directory_fd)
            restored = _publication_target_precondition_at(
                directory_fd,
                candidate_name,
                display_path=display_path.with_name(candidate_name),
            )
            if restored != expected:
                raise ValueError("receipt collision rollback writer mismatch")
    else:
        candidate_metadata = _lstat_at(directory_fd, candidate_name)
        current = _publication_target_precondition_at(
            directory_fd, filename, display_path=display_path
        )
        if candidate_metadata is not None:
            candidate_is_ours = (
                candidate_metadata.st_dev,
                candidate_metadata.st_ino,
            ) == published_identity
            candidate_precondition = _publication_target_precondition_at(
                directory_fd,
                candidate_name,
                display_path=display_path.with_name(candidate_name),
            )
            if (
                current == expected
                and not candidate_is_ours
                and candidate_precondition != expected
            ):
                _renameat2_at(
                    directory_fd,
                    candidate_name,
                    filename,
                    _RENAME_EXCHANGE,
                )
                os.fsync(directory_fd)
                candidate_precondition = _publication_target_precondition_at(
                    directory_fd,
                    candidate_name,
                    display_path=display_path.with_name(candidate_name),
                )
            if not candidate_is_ours and candidate_precondition != expected:
                raise ValueError("receipt collision rollback state is ambiguous")
    candidate_metadata = _lstat_at(directory_fd, candidate_name)
    if candidate_metadata is not None:
        os.unlink(candidate_name, dir_fd=directory_fd)
        os.fsync(directory_fd)
    return _publication_target_precondition_at(
        directory_fd, filename, display_path=display_path
    )


def _bounded_direct_finalization_text(
    value: object, *, maximum_bytes: int
) -> tuple[str | None, bool]:
    if not isinstance(value, str):
        return None, False
    try:
        encoded = value.encode("utf-8")
    except UnicodeError:
        return None, True
    if len(encoded) <= maximum_bytes:
        return value, False
    return encoded[:maximum_bytes].decode("utf-8", errors="ignore"), True


def _compact_direct_finalization_result(
    result: Mapping[str, Any],
    *,
    compaction_reason: str,
    original_result_sha256: str | None,
    original_result_bytes: int | None,
) -> dict[str, Any]:
    verdict, _verdict_truncated = _bounded_direct_finalization_text(
        result.get("verdict"), maximum_bytes=256
    )
    verification_status, _status_truncated = _bounded_direct_finalization_text(
        result.get("verification_status"), maximum_bytes=256
    )
    repair_hints, repair_hints_truncated = _bounded_direct_finalization_text(
        result.get("repair_hints"),
        maximum_bytes=_MAX_DIRECT_FINALIZATION_REPAIR_HINT_BYTES,
    )
    report = result.get("verification_report")
    summary = report.get("summary") if isinstance(report, Mapping) else None
    verification_summary, verification_summary_truncated = (
        _bounded_direct_finalization_text(
            summary,
            maximum_bytes=_MAX_DIRECT_FINALIZATION_SUMMARY_BYTES,
        )
    )
    return {
        "published": False,
        "durable_result_status": "compacted",
        "compaction_reason": compaction_reason,
        "original_result_sha256": original_result_sha256,
        "original_result_bytes": original_result_bytes,
        "verdict": verdict,
        "verification_status": verification_status,
        "repair_hints": repair_hints,
        "repair_hints_truncated": repair_hints_truncated,
        "verification_summary": verification_summary,
        "verification_summary_truncated": verification_summary_truncated,
    }


def _direct_finalization_result_record(
    *,
    intent: Mapping[str, Any],
    dispatch: Mapping[str, Any],
    result: Mapping[str, Any],
) -> dict[str, Any]:
    if result.get("published") is not False:
        raise ValueError("direct nonpublication result is invalid")
    durable_result = dict(result)
    try:
        original_raw = json.dumps(
            durable_result,
            allow_nan=False,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        original_result_sha256 = None
        original_result_bytes = None
        result_encoding = "compacted"
        stored_result = _compact_direct_finalization_result(
            durable_result,
            compaction_reason="noncanonical",
            original_result_sha256=None,
            original_result_bytes=None,
        )
    else:
        original_result_sha256 = hashlib.sha256(original_raw).hexdigest()
        original_result_bytes = len(original_raw)
        result_encoding = "complete"
        stored_result = durable_result

    completed_at_utc = datetime.now(timezone.utc).isoformat()

    def build() -> dict[str, Any]:
        return {
            "schema_version": _DIRECT_FINALIZATION_RESULT_SCHEMA,
            "status": "completed",
            "intent_sha256": hashlib.sha256(
                _canonical_json_line_bytes(intent)
            ).hexdigest(),
            "dispatch_sha256": hashlib.sha256(
                _canonical_json_line_bytes(dispatch)
            ).hexdigest(),
            "result_encoding": result_encoding,
            "original_result_sha256": original_result_sha256,
            "original_result_bytes": original_result_bytes,
            "max_result_bytes": _MAX_DIRECT_FINALIZATION_RESULT_BYTES,
            "max_repair_hint_bytes": (
                _MAX_DIRECT_FINALIZATION_REPAIR_HINT_BYTES
            ),
            "max_summary_bytes": _MAX_DIRECT_FINALIZATION_SUMMARY_BYTES,
            "result": stored_result,
            "completed_at_utc": completed_at_utc,
        }

    record = build()
    if len(_canonical_json_line_bytes(record)) > (
        _MAX_DIRECT_FINALIZATION_RESULT_BYTES
    ):
        assert original_result_sha256 is not None
        assert original_result_bytes is not None
        result_encoding = "compacted"
        stored_result = _compact_direct_finalization_result(
            durable_result,
            compaction_reason="over_limit",
            original_result_sha256=original_result_sha256,
            original_result_bytes=original_result_bytes,
        )
        record = build()
    if len(_canonical_json_line_bytes(record)) > (
        _MAX_DIRECT_FINALIZATION_RESULT_BYTES
    ):
        raise ValueError("direct compact result exceeds its byte cap")
    return record


def _prepare_direct_finalization(
    *,
    receipt_path: Path,
    intent_seed: dict[str, Any],
    journal_parent: Path | None = None,
    journal_parent_fd: int | None = None,
) -> tuple[dict[str, Any], Path, Path, Path, dict[str, Any] | None]:
    proof_sha256 = intent_seed.get("proof_digest")
    if not isinstance(proof_sha256, str) or _HEX_DIGEST_RE.fullmatch(
        proof_sha256
    ) is None:
        raise ValueError("direct finalization proof digest is invalid")
    journal_key = _direct_finalization_journal_key(intent_seed)
    current_paths = _direct_finalization_paths(
        receipt_path, journal_key, journal_parent=journal_parent
    )
    intent_path, dispatch_path, result_path = current_paths
    intent = _read_direct_finalization_record(
        intent_path,
        maximum_bytes=_ABSOLUTE_MAX_DIRECT_FINALIZATION_INTENT_BYTES,
        directory_fd=journal_parent_fd,
    )
    dispatch = _read_direct_finalization_record(
        dispatch_path,
        maximum_bytes=_ABSOLUTE_MAX_DIRECT_FINALIZATION_INTENT_BYTES,
        directory_fd=journal_parent_fd,
    )
    result_record = _read_direct_finalization_record(
        result_path,
        maximum_bytes=_ABSOLUTE_MAX_DIRECT_FINALIZATION_RESULT_BYTES,
        directory_fd=journal_parent_fd,
    )
    using_legacy_paths = False
    using_prior_semantic_paths = False
    if intent is None and dispatch is None and result_record is None:
        prior_semantic_paths = _direct_finalization_paths(
            receipt_path,
            _direct_finalization_journal_key_v1(intent_seed),
            journal_parent=journal_parent,
        )
        if prior_semantic_paths != current_paths:
            prior_intent = _read_direct_finalization_record(
                prior_semantic_paths[0],
                maximum_bytes=_ABSOLUTE_MAX_DIRECT_FINALIZATION_INTENT_BYTES,
                directory_fd=journal_parent_fd,
            )
            prior_dispatch = _read_direct_finalization_record(
                prior_semantic_paths[1],
                maximum_bytes=_ABSOLUTE_MAX_DIRECT_FINALIZATION_INTENT_BYTES,
                directory_fd=journal_parent_fd,
            )
            prior_result = _read_direct_finalization_record(
                prior_semantic_paths[2],
                maximum_bytes=_ABSOLUTE_MAX_DIRECT_FINALIZATION_RESULT_BYTES,
                directory_fd=journal_parent_fd,
            )
            if any(
                value is not None
                for value in (prior_intent, prior_dispatch, prior_result)
            ):
                intent_path, dispatch_path, result_path = prior_semantic_paths
                intent = prior_intent
                dispatch = prior_dispatch
                result_record = prior_result
                using_prior_semantic_paths = True
    if intent is None and dispatch is None and result_record is None:
        legacy_paths = _legacy_direct_finalization_paths(
            receipt_path, proof_sha256
        )
        if (
            legacy_paths != current_paths
            and _legacy_direct_finalization_paths_are_addressable(legacy_paths)
        ):
            snapshot_path = _direct_legacy_snapshot_path(current_paths[0])
            legacy_snapshot = _read_direct_legacy_snapshot(
                snapshot_path=snapshot_path,
                journal_parent_fd=journal_parent_fd,
                journal_key=journal_key,
                receipt_path=receipt_path,
                proof_sha256=proof_sha256,
            )
            if legacy_snapshot is None:
                legacy_intent = _read_direct_finalization_record(
                    legacy_paths[0],
                    maximum_bytes=(
                        _ABSOLUTE_MAX_DIRECT_FINALIZATION_INTENT_BYTES
                    ),
                )
                legacy_dispatch = _read_direct_finalization_record(
                    legacy_paths[1],
                    maximum_bytes=(
                        _ABSOLUTE_MAX_DIRECT_FINALIZATION_INTENT_BYTES
                    ),
                )
                legacy_result = _read_direct_finalization_record(
                    legacy_paths[2],
                    maximum_bytes=(
                        _ABSOLUTE_MAX_DIRECT_FINALIZATION_RESULT_BYTES
                    ),
                )
                if any(
                    value is not None
                    for value in (
                        legacy_intent,
                        legacy_dispatch,
                        legacy_result,
                    )
                ):
                    legacy_snapshot = _commit_direct_legacy_snapshot(
                        snapshot_path=snapshot_path,
                        journal_parent_fd=journal_parent_fd,
                        journal_key=journal_key,
                        receipt_path=receipt_path,
                        proof_sha256=proof_sha256,
                        legacy_intent=legacy_intent,
                        legacy_dispatch=legacy_dispatch,
                        legacy_result=legacy_result,
                    )
            if legacy_snapshot is not None:
                (
                    legacy_intent,
                    legacy_dispatch,
                    legacy_result,
                ) = legacy_snapshot
            else:
                legacy_intent = legacy_dispatch = legacy_result = None
            if any(
                value is not None
                for value in (legacy_intent, legacy_dispatch, legacy_result)
            ):
                intent_path, dispatch_path, result_path = legacy_paths
                intent = legacy_intent
                dispatch = legacy_dispatch
                result_record = legacy_result
                using_legacy_paths = True

    def start_current_intent() -> tuple[
        dict[str, Any], Path, Path, Path, None
    ]:
        current_intent = {
            **intent_seed,
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
        }
        _write_direct_finalization_record(
            current_paths[0],
            current_intent,
            maximum_bytes=_MAX_DIRECT_FINALIZATION_INTENT_BYTES,
            directory_fd=journal_parent_fd,
        )
        return (
            current_intent,
            current_paths[0],
            current_paths[1],
            current_paths[2],
            None,
        )

    if intent is None:
        if dispatch is not None or result_record is not None:
            raise ValueError("direct finalization effect lacks its intent")
        return start_current_intent()

    expected_keys = {*intent_seed, "created_at_utc"}
    legacy_keys = (
        expected_keys
        - {
            "checked_item_count",
            "checked_item_ids_sha256",
            "client_source_sha256",
            "publication_generation_parent_sha256",
            "max_intent_bytes",
        }
    ) | {"checked_item_ids"}
    intent_schema = intent.get("schema_version")
    current_intent = intent_schema == _DIRECT_FINALIZATION_INTENT_SCHEMA
    legacy_intent = (
        intent_schema == _DIRECT_FINALIZATION_INTENT_SCHEMA_LEGACY
    )
    prior_semantic_keys = expected_keys - {
        "publication_generation_parent_sha256"
    }
    if (
        (
            current_intent
            and set(intent)
            != (prior_semantic_keys if using_prior_semantic_paths else expected_keys)
        )
        or (legacy_intent and set(intent) != legacy_keys)
        or not (current_intent or legacy_intent)
        or intent.get("status") != "submitted"
        or intent.get("proof_digest") != proof_sha256
    ):
        raise ValueError("direct finalization intent binding mismatch")
    if current_intent:
        if (
            isinstance(intent.get("max_intent_bytes"), bool)
            or not isinstance(intent.get("max_intent_bytes"), int)
            or not 0 < intent["max_intent_bytes"]
            <= _ABSOLUTE_MAX_DIRECT_FINALIZATION_INTENT_BYTES
            or len(_canonical_json_line_bytes(intent))
            > intent["max_intent_bytes"]
        ):
            raise ValueError("direct finalization intent binding mismatch")
    else:
        checked_item_ids = intent.get("checked_item_ids")
        if (
            not isinstance(checked_item_ids, list)
            or any(not isinstance(item_id, str) for item_id in checked_item_ids)
            or len(checked_item_ids) != intent_seed["checked_item_count"]
            or hashlib.sha256(
                _canonical_json_line_bytes(checked_item_ids)
            ).hexdigest()
            != intent_seed["checked_item_ids_sha256"]
        ):
            raise ValueError("legacy direct finalization item binding mismatch")
    _validate_publication_target_precondition(
        intent.get("publication_target_precondition")
    )
    try:
        created_at = datetime.fromisoformat(intent["created_at_utc"])
    except (TypeError, ValueError) as exc:
        raise ValueError("direct finalization intent timestamp is invalid") from exc
    if (
        created_at.tzinfo is None
        or created_at.utcoffset() != timedelta(0)
        or intent["created_at_utc"]
        != created_at.astimezone(timezone.utc).isoformat()
    ):
        raise ValueError("direct finalization intent timestamp is invalid")
    intent_sha256 = hashlib.sha256(
        _canonical_json_line_bytes(intent)
    ).hexdigest()
    immutable_replay_fields = set(
        _DIRECT_FINALIZATION_JOURNAL_IDENTITY_FIELDS
    ) - {"checked_item_count", "checked_item_ids_sha256"}
    if legacy_intent:
        immutable_replay_fields.discard("client_source_sha256")
        immutable_replay_fields.discard(
            "publication_generation_parent_sha256"
        )
    if using_prior_semantic_paths:
        immutable_replay_fields.discard(
            "publication_generation_parent_sha256"
        )
    replay_context_matches = all(
        intent.get(field) == intent_seed.get(field)
        for field in immutable_replay_fields
    )
    if current_intent:
        replay_context_matches = replay_context_matches and all(
            intent.get(field) == intent_seed.get(field)
            for field in {"checked_item_count", "checked_item_ids_sha256"}
        )
    if dispatch is not None:
        expected_dispatch_keys = {
            "schema_version", "status", "intent_sha256", "dispatched_at_utc",
        }
        if (
            set(dispatch) != expected_dispatch_keys
            or dispatch.get("schema_version")
            != _DIRECT_FINALIZATION_DISPATCH_SCHEMA
            or dispatch.get("status") != "dispatched"
            or dispatch.get("intent_sha256") != intent_sha256
        ):
            raise ValueError("direct finalization dispatch binding mismatch")
    if result_record is not None:
        if dispatch is None:
            raise ValueError("direct finalization result lacks its dispatch")
        legacy_result_keys = {
            "schema_version", "status", "intent_sha256", "dispatch_sha256",
            "result", "completed_at_utc",
        }
        current_result_keys = legacy_result_keys | {
            "result_encoding", "original_result_sha256",
            "original_result_bytes", "max_result_bytes",
            "max_repair_hint_bytes", "max_summary_bytes",
        }
        dispatch_sha256 = hashlib.sha256(
            _canonical_json_line_bytes(dispatch)
        ).hexdigest()
        if (
            set(result_record) not in (legacy_result_keys, current_result_keys)
            or result_record.get("status") != "completed"
            or result_record.get("intent_sha256") != intent_sha256
            or result_record.get("dispatch_sha256") != dispatch_sha256
            or not isinstance(result_record.get("result"), dict)
            or result_record["result"].get("published") is not False
        ):
            raise ValueError("direct finalization result binding mismatch")
        schema = result_record.get("schema_version")
        if schema == _DIRECT_FINALIZATION_RESULT_SCHEMA_LEGACY:
            if set(result_record) != legacy_result_keys:
                raise ValueError("direct finalization result binding mismatch")
        elif (
            schema != _DIRECT_FINALIZATION_RESULT_SCHEMA
            or set(result_record) != current_result_keys
            or result_record.get("result_encoding")
            not in {"complete", "compacted"}
        ):
            raise ValueError("direct finalization result binding mismatch")
        else:
            max_result_bytes = result_record.get("max_result_bytes")
            max_repair_hint_bytes = result_record.get("max_repair_hint_bytes")
            max_summary_bytes = result_record.get("max_summary_bytes")
            if (
                isinstance(max_result_bytes, bool)
                or not isinstance(max_result_bytes, int)
                or not 0 < max_result_bytes
                <= _ABSOLUTE_MAX_DIRECT_FINALIZATION_RESULT_BYTES
                or isinstance(max_repair_hint_bytes, bool)
                or not isinstance(max_repair_hint_bytes, int)
                or not 0 < max_repair_hint_bytes
                <= _ABSOLUTE_MAX_DIRECT_FINALIZATION_TEXT_BYTES
                or isinstance(max_summary_bytes, bool)
                or not isinstance(max_summary_bytes, int)
                or not 0 < max_summary_bytes
                <= _ABSOLUTE_MAX_DIRECT_FINALIZATION_TEXT_BYTES
                or len(_canonical_json_line_bytes(result_record))
                > max_result_bytes
            ):
                raise ValueError("direct finalization result cap mismatch")
            stored_result = result_record["result"]
            if result_record["result_encoding"] == "complete":
                raw_result = json.dumps(
                    stored_result,
                    allow_nan=False,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
                if (
                    result_record.get("original_result_sha256")
                    != hashlib.sha256(raw_result).hexdigest()
                    or result_record.get("original_result_bytes")
                    != len(raw_result)
                ):
                    raise ValueError(
                        "direct finalization result digest mismatch"
                    )
            elif (
                stored_result.get("durable_result_status") != "compacted"
                or stored_result.get("compaction_reason")
                not in {"over_limit", "noncanonical"}
                or stored_result.get("original_result_sha256")
                != result_record.get("original_result_sha256")
                or stored_result.get("original_result_bytes")
                != result_record.get("original_result_bytes")
            ):
                raise ValueError("direct compact result binding mismatch")
            else:
                for field, maximum in (
                    ("repair_hints", max_repair_hint_bytes),
                    ("verification_summary", max_summary_bytes),
                ):
                    text = stored_result.get(field)
                    if text is not None and (
                        not isinstance(text, str)
                        or len(text.encode("utf-8")) > maximum
                    ):
                        raise ValueError("direct compact result text mismatch")
        if using_prior_semantic_paths and (
            intent_seed.get("publication_generation_parent_sha256")
            is not None
            or not replay_context_matches
        ):
            return start_current_intent()
        if not replay_context_matches and (
            using_legacy_paths
        ):
            return start_current_intent()
        if not replay_context_matches:
            raise ValueError("direct finalization replay context mismatch")
        return intent, intent_path, dispatch_path, result_path, dict(
            result_record["result"]
        )
    if dispatch is not None:
        if using_prior_semantic_paths and intent_seed.get(
            "publication_generation_parent_sha256"
        ) is not None:
            return start_current_intent()
        if not replay_context_matches and (
            using_legacy_paths
        ):
            return start_current_intent()
        if not replay_context_matches:
            raise ValueError("direct finalization replay context mismatch")
        raise VerificationExecutionUnknown(
            "direct verifier dispatch has no durable terminal result"
        )
    if using_legacy_paths or using_prior_semantic_paths:
        return start_current_intent()
    if any(
        intent.get(key) != expected for key, expected in intent_seed.items()
    ):
        # No verifier effect crossed dispatch, so a deployment/profile/target
        # change may safely replace this predispatch admission under the held
        # per-target execution lock.
        intent = {
            **intent_seed,
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
        }
        _write_direct_finalization_record(
            intent_path,
            intent,
            maximum_bytes=_MAX_DIRECT_FINALIZATION_INTENT_BYTES,
            replace_existing=True,
            existing_maximum_bytes=(
                _ABSOLUTE_MAX_DIRECT_FINALIZATION_INTENT_BYTES
            ),
            directory_fd=journal_parent_fd,
        )
    return intent, intent_path, dispatch_path, result_path, None


def _commit_direct_finalization_dispatch(
    *,
    intent: Mapping[str, Any],
    dispatch_path: Path,
    journal_parent_fd: int | None = None,
) -> dict[str, Any]:
    dispatch = {
        "schema_version": _DIRECT_FINALIZATION_DISPATCH_SCHEMA,
        "status": "dispatched",
        "intent_sha256": hashlib.sha256(
            _canonical_json_line_bytes(intent)
        ).hexdigest(),
        "dispatched_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    return _write_direct_finalization_record(
        dispatch_path,
        dispatch,
        maximum_bytes=_MAX_DIRECT_FINALIZATION_INTENT_BYTES,
        directory_fd=journal_parent_fd,
    )


def _commit_direct_finalization_result(
    *,
    intent: Mapping[str, Any],
    dispatch_path: Path,
    result_path: Path,
    result: Mapping[str, Any],
    journal_parent_fd: int | None = None,
) -> dict[str, Any]:
    dispatch = _read_direct_finalization_record(
        dispatch_path,
        maximum_bytes=_MAX_DIRECT_FINALIZATION_INTENT_BYTES,
        directory_fd=journal_parent_fd,
    )
    if dispatch is None:
        raise ValueError("direct finalization result lacks its dispatch")
    record = _direct_finalization_result_record(
        intent=intent,
        dispatch=dispatch,
        result=result,
    )
    _write_direct_finalization_record(
        result_path,
        record,
        maximum_bytes=_MAX_DIRECT_FINALIZATION_RESULT_BYTES,
        directory_fd=journal_parent_fd,
    )
    return dict(record["result"])


def proof_digest(proof: str) -> str:
    return hashlib.sha256(proof.encode("utf-8")).hexdigest()


def expected_attestation(
    *,
    proof: str,
    statement: str,
) -> tuple[list[str], str]:
    manifest = parse_blueprint(proof, target_statement=statement)
    return list(manifest.item_ids), aggregate_context_digest(manifest)


def _validate_findings(value: object, name: str) -> list[dict[str, str]]:
    if not isinstance(value, list):
        raise ValueError(f"{name} must be a list")
    for index, finding in enumerate(value):
        if not isinstance(finding, dict) or set(finding) != _FINDING_FIELDS:
            raise ValueError(
                f"{name}[{index}] must contain exactly location and issue"
            )
        if any(
            not isinstance(finding[field], str) or not finding[field]
            for field in _FINDING_FIELDS
        ):
            raise ValueError(f"{name}[{index}] fields must be non-empty strings")
    return value


def validate_service_response(
    payload: object,
    *,
    expected_proof_digest: str,
    expected_checked_item_ids: list[str],
    expected_context_digest: str,
    expected_manifest: ProofManifest,
    expected_verification_attempt_id: str,
    expected_verification_pass_index: int,
) -> Dict[str, Any]:
    if not isinstance(payload, dict) or set(payload) != _OUTPUT_FIELDS:
        raise ValueError("verification service returned an invalid output shape")

    if payload["output_schema_version"] != 2:
        raise ValueError("verification service returned an unsupported output schema")
    if payload["verification_status"] != "final":
        raise ValueError("verification service did not return a final aggregate verdict")
    if payload["needs_expanded_proofs"] != []:
        raise ValueError("final aggregate verdict must not request expanded proofs")
    if payload["verification_attempt_id"] != expected_verification_attempt_id:
        raise ValueError("verification service attempt id does not match the request")
    expected_role = (
        "primary"
        if expected_verification_pass_index == 1
        else "adversarial_full_claim_audit"
    )
    if (
        isinstance(payload["verification_pass_index"], bool)
        or not isinstance(payload["verification_pass_index"], int)
        or payload["verification_pass_index"] != expected_verification_pass_index
        or payload["verification_role"] != expected_role
    ):
        raise ValueError("verification service pass role does not match the request")
    if (
        _VERIFICATION_ATTEMPT_RE.fullmatch(expected_verification_attempt_id) is None
        or not isinstance(payload["verifier_run_id"], str)
        or _VERIFIER_RUN_ID_RE.fullmatch(payload["verifier_run_id"]) is None
        or not isinstance(payload["verifier_model"], str)
        or not payload["verifier_model"].strip()
        or not isinstance(payload["verifier_reasoning_effort"], str)
        or payload["verifier_reasoning_effort"]
        not in {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}
        or not isinstance(payload["verifier_service_version"], str)
        or not payload["verifier_service_version"].strip()
    ):
        raise ValueError("verification service provenance is invalid")

    report = payload["verification_report"]
    if not isinstance(report, dict) or set(report) != _REPORT_FIELDS:
        raise ValueError("verification_report has an invalid output shape")
    if not isinstance(report["summary"], str):
        raise ValueError("verification_report.summary must be a string")
    critical_errors = _validate_findings(
        report["critical_errors"], "verification_report.critical_errors"
    )
    gaps = _validate_findings(report["gaps"], "verification_report.gaps")

    verdict = payload["verdict"]
    repair_hints = payload["repair_hints"]
    if verdict not in {"correct", "wrong"}:
        raise ValueError("verification service returned an unknown verdict")
    if not isinstance(repair_hints, str):
        raise ValueError("repair_hints must be a string")
    has_findings = bool(critical_errors or gaps)
    if verdict == "correct" and (has_findings or repair_hints != ""):
        raise ValueError("correct verdict is inconsistent with findings or hints")
    if verdict == "wrong" and (not has_findings or not repair_hints.strip()):
        raise ValueError("wrong verdict requires findings and repair hints")

    checked_item_ids = payload["checked_item_ids"]
    if (
        not isinstance(checked_item_ids, list)
        or any(
            not isinstance(item_id, str) or _ITEM_ID_RE.fullmatch(item_id) is None
            for item_id in checked_item_ids
        )
        or len(set(checked_item_ids)) != len(checked_item_ids)
    ):
        raise ValueError("checked_item_ids must be a unique list of proof-item ids")
    if checked_item_ids != expected_checked_item_ids:
        raise ValueError("checked_item_ids does not exactly match the blueprint manifest")

    if (
        not isinstance(payload["proof_digest"], str)
        or _HEX_DIGEST_RE.fullmatch(payload["proof_digest"]) is None
        or payload["proof_digest"] != expected_proof_digest
    ):
        raise ValueError("verification service proof_digest does not match the draft")
    if (
        not isinstance(payload["context_digest"], str)
        or _HEX_DIGEST_RE.fullmatch(payload["context_digest"]) is None
        or payload["context_digest"] != expected_context_digest
    ):
        raise ValueError("verification service context_digest does not match the manifest")

    attestations = payload["item_context_attestations"]
    if not isinstance(attestations, list) or len(attestations) != len(
        expected_checked_item_ids
    ):
        raise ValueError("item_context_attestations must cover the exact manifest")
    rebuilt_attestations: list[dict[str, Any]] = []
    for index, (item_id, attestation) in enumerate(
        zip(expected_checked_item_ids, attestations, strict=True)
    ):
        path = f"item_context_attestations[{index}]"
        if not isinstance(attestation, dict) or set(attestation) != _ITEM_CONTEXT_ATTESTATION_FIELDS:
            raise ValueError(f"{path} has an invalid shape")
        if attestation["item_id"] != item_id:
            raise ValueError(f"{path}.item_id does not match manifest order")
        disposition = attestation["disposition"]
        if disposition not in {"verified", "blocked"}:
            raise ValueError(f"{path}.disposition is invalid")
        final_round = attestation["final_round"]
        if (
            isinstance(final_round, bool)
            or not isinstance(final_round, int)
            or final_round < 0
            or final_round > MAX_EXPANSION_ROUNDS
        ):
            raise ValueError(f"{path}.final_round is invalid")
        expanded_ids = attestation["expanded_proof_ids"]
        if (
            not isinstance(expanded_ids, list)
            or any(
                not isinstance(expanded_id, str)
                or _ITEM_ID_RE.fullmatch(expanded_id) is None
                for expanded_id in expanded_ids
            )
            or len(set(expanded_ids)) != len(expanded_ids)
            or len(expanded_ids) > MAX_EXPANDED_PROOFS
        ):
            raise ValueError(f"{path}.expanded_proof_ids is invalid")
        max_chars = attestation["max_chars"]
        if (
            isinstance(max_chars, bool)
            or not isinstance(max_chars, int)
            or max_chars <= 0
            or max_chars > VERIFY_CONTEXT_MAX_CHARS
        ):
            raise ValueError(f"{path}.max_chars is invalid")
        attested_digest = attestation["context_digest"]
        if (
            not isinstance(attested_digest, str)
            or _HEX_DIGEST_RE.fullmatch(attested_digest) is None
        ):
            raise ValueError(f"{path}.context_digest is invalid")
        item_verdict = attestation["verdict"]
        if item_verdict not in {"correct", "wrong"}:
            raise ValueError(f"{path}.verdict is invalid")
        if disposition == "blocked" and (
            final_round != 0 or expanded_ids or item_verdict != "wrong"
        ):
            raise ValueError(f"{path} has an invalid blocked disposition")
        if (final_round == 0) != (expanded_ids == []):
            raise ValueError(f"{path} has inconsistent round and expansion ids")
        try:
            rebuilt = build_item_context(
                expected_manifest,
                item_id,
                max_chars=max_chars,
                expanded_proof_ids=expanded_ids,
                round_index=final_round,
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{path} cannot be rebuilt from the manifest") from exc
        if (
            rebuilt["complete"] is not True
            or rebuilt["truncated"] is not False
            or rebuilt["missing"]
            or rebuilt["omitted"]
            or rebuilt["digest"] != attested_digest
            or rebuilt["expanded_proof_characters"]
            > MAX_EXPANDED_PROOF_CHARS
        ):
            raise ValueError(f"{path} does not match the authenticated manifest context")
        rebuilt_attestations.append(dict(attestation))

    if verdict == "correct" and any(
        attestation["disposition"] != "verified"
        or attestation["verdict"] != "correct"
        for attestation in rebuilt_attestations
    ):
        raise ValueError("correct aggregate verdict contains failed or blocked items")
    adaptive_digest = payload["adaptive_context_digest"]
    expected_adaptive_digest = aggregate_adaptive_context_digest(
        expected_manifest, rebuilt_attestations
    )
    if (
        not isinstance(adaptive_digest, str)
        or _HEX_DIGEST_RE.fullmatch(adaptive_digest) is None
        or adaptive_digest != expected_adaptive_digest
    ):
        raise ValueError("adaptive_context_digest does not match rebuilt item contexts")

    return payload


def _parse_targeted_manifest(statement: str, proof: str) -> ProofManifest:
    try:
        return parse_blueprint(proof)
    except ValueError:
        return parse_blueprint(proof, target_statement=statement)


def _validate_targeted_verification_limits(value: object) -> dict[str, int]:
    if (
        not isinstance(value, dict)
        or set(value) != _TARGETED_VERIFICATION_LIMIT_FIELDS
    ):
        raise ValueError("targeted verification limits have an invalid shape")
    limits = dict(value)
    bounds = {
        "context_max_chars": (1, ABSOLUTE_VERIFY_CONTEXT_MAX_CHARS),
        "max_expansion_rounds": (0, ABSOLUTE_MAX_EXPANSION_ROUNDS),
        "max_expanded_proofs": (0, ABSOLUTE_MAX_EXPANDED_PROOFS),
        "max_expanded_proof_chars": (
            1,
            ABSOLUTE_MAX_EXPANDED_PROOF_CHARS,
        ),
    }
    for name, (minimum, maximum) in bounds.items():
        observed = limits.get(name)
        if (
            isinstance(observed, bool)
            or not isinstance(observed, int)
            or not minimum <= observed <= maximum
        ):
            raise ValueError(f"targeted verification limit {name} is invalid")
    return limits


def _validate_targeted_execution_binding(value: object) -> dict[str, Any]:
    if (
        not isinstance(value, dict)
        or set(value) != _TARGETED_EXECUTION_BINDING_FIELDS
    ):
        raise ValueError("targeted execution binding has an invalid shape")
    backend = value.get("backend")
    prompt_limits = value.get("prompt_limits")
    schema_version = value.get("schema_version")
    expected_prompt_limit_fields = (
        _TARGETED_PROMPT_LIMIT_V1_FIELDS
        if schema_version == "rethlas_targeted_execution_binding_v1"
        else _TARGETED_PROMPT_LIMIT_FIELDS
        if schema_version
        in {
            "rethlas_targeted_execution_binding_v2",
            "rethlas_targeted_execution_binding_v3",
        }
        else None
    )
    if (
        expected_prompt_limit_fields is None
        or not isinstance(value.get("service_version"), str)
        or not value["service_version"]
        or len(value["service_version"]) > 128
        or any(
            not isinstance(value.get(name), str)
            or _HEX_DIGEST_RE.fullmatch(value[name]) is None
            for name in ("closure_sha256", "prompt_contract_sha256")
        )
        or not isinstance(backend, dict)
        or set(backend) != _TARGETED_BACKEND_BINDING_FIELDS
        or backend.get("adapter") not in {"codex_cli", "claude_cli"}
        or backend.get("provider")
        not in {"openai", "anthropic", "vertex", "bedrock", "foundry"}
        or backend.get("reasoning_effort")
        not in {"low", "medium", "high", "xhigh", "max"}
        or any(
            not isinstance(backend.get(name), str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:@\[\]-]{0,127}", backend[name])
            is None
            for name in ("model", "launch_model")
        )
        or not isinstance(prompt_limits, dict)
        or set(prompt_limits) != expected_prompt_limit_fields
        or any(
            isinstance(prompt_limits.get(name), bool)
            or not isinstance(prompt_limits.get(name), int)
            or not 0 < prompt_limits[name] <= 1_000_000_000
            for name in expected_prompt_limit_fields
        )
        or prompt_limits.get("max_targeted_receipt_bytes", 8_000_001)
        > 8_000_000
    ):
        raise ValueError("targeted execution binding is invalid")
    return json.loads(
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )


def validate_targeted_claim_receipt(
    payload: object,
    *,
    ticket: Dict[str, Any],
    statement: str,
    proof: str,
    verification_deadline_utc: str,
    expected_proof_context: Mapping[str, Any] | None = None,
) -> Dict[str, Any]:
    """Validate one nonpublishing verifier receipt against local source bytes."""

    if not isinstance(payload, dict) or set(payload) != _TARGETED_RECEIPT_FIELDS:
        raise ValueError("targeted verifier returned an invalid receipt shape")
    if payload["schema_version"] != TARGETED_RECEIPT_SCHEMA:
        raise ValueError("targeted verifier returned an unsupported receipt schema")
    receipt_proof_context = _validate_proof_context_binding(
        payload.get("proof_context")
    )
    execution_binding = _validate_targeted_execution_binding(
        payload.get("execution_binding")
    )
    if expected_proof_context is None:
        expected_context_binding = _current_proof_context_binding()
    else:
        expected_context_binding = _validate_proof_context_binding(
            expected_proof_context
        )
    if receipt_proof_context != expected_context_binding:
        raise ValueError("targeted verifier proof-context binding mismatch")
    try:
        current_context_binding = _current_proof_context_binding()
    except RuntimeError:
        current_context_binding = None
    rebuild_with_current_parser = receipt_proof_context == current_context_binding
    claim = ticket.get("claim")
    if not isinstance(claim, dict):
        raise ValueError("targeted verifier ticket lacks an exact claim")
    expected = {
        "ticket_id": ticket.get("ticket_id"),
        "review_id": ticket.get("review_id"),
        "snapshot_sha256": ticket.get("snapshot_sha256"),
        "route_id": ticket.get("route_id"),
        "blueprint_sha256": ticket.get("blueprint_sha256"),
        "blueprint_item_id": ticket.get("blueprint_item_id"),
        "blueprint_item_label": claim.get("blueprint_item_label"),
        "claim_sha256": claim.get("claim_sha256"),
    }
    if any(payload[key] != value for key, value in expected.items()):
        raise ValueError("targeted verifier receipt binding mismatch")
    try:
        deadline = datetime.fromisoformat(verification_deadline_utc)
    except (TypeError, ValueError) as exc:
        raise ValueError("targeted verification deadline is invalid") from exc
    if (
        deadline.tzinfo is None
        or deadline.utcoffset() != timedelta(0)
        or verification_deadline_utc
        != deadline.astimezone(timezone.utc).isoformat()
        or payload["verification_deadline_utc"] != verification_deadline_utc
    ):
        raise ValueError("targeted verifier receipt deadline binding mismatch")
    observed_blueprint_sha = proof_digest(proof)
    if observed_blueprint_sha != expected["blueprint_sha256"]:
        raise ValueError("targeted verifier blueprint changed before response validation")
    manifest: ProofManifest | None = None
    item: Any | None = None
    if rebuild_with_current_parser:
        manifest = _parse_targeted_manifest(statement, proof)
        item_matches = [
            candidate
            for candidate in manifest.items
            if candidate.label == expected["blueprint_item_label"]
        ]
        if len(item_matches) != 1:
            raise ValueError("targeted verifier claim label is not unique in blueprint")
        item = item_matches[0]
        if (
            item.item_id != expected["blueprint_item_id"]
            or item.digest != expected["claim_sha256"]
        ):
            raise ValueError(
                "targeted verifier claim commitment disagrees with blueprint"
            )
    if payload["verification_status"] != "final" or payload["verdict"] not in {
        "correct",
        "wrong",
    }:
        raise ValueError("targeted verifier receipt lacks a final verdict")
    report = payload["verification_report"]
    if not isinstance(report, dict) or set(report) != _REPORT_FIELDS:
        raise ValueError("targeted verification_report has an invalid shape")
    if not isinstance(report["summary"], str):
        raise ValueError("targeted verification_report summary must be a string")
    critical_errors = _validate_findings(
        report["critical_errors"], "targeted verification_report.critical_errors"
    )
    gaps = _validate_findings(report["gaps"], "targeted verification_report.gaps")
    repair_hints = payload["repair_hints"]
    if not isinstance(repair_hints, str):
        raise ValueError("targeted repair_hints must be a string")
    has_findings = bool(critical_errors or gaps)
    if payload["verdict"] == "correct" and (has_findings or repair_hints != ""):
        raise ValueError("targeted correct verdict conflicts with findings")
    if payload["verdict"] == "wrong" and (not has_findings or not repair_hints.strip()):
        raise ValueError("targeted wrong verdict requires findings and repair hints")
    expected_item_id = str(expected["blueprint_item_id"])
    if payload["checked_item_ids"] != [expected_item_id]:
        raise ValueError("targeted verifier checked an unexpected item set")
    limits = _validate_targeted_verification_limits(
        payload["verification_limits"]
    )
    attestation = payload["context_attestation"]
    if not isinstance(attestation, dict) or set(attestation) != _ITEM_CONTEXT_ATTESTATION_FIELDS:
        raise ValueError("targeted verifier context attestation has an invalid shape")
    if (
        attestation["item_id"] != expected_item_id
        or attestation["disposition"] != "verified"
        or attestation["verdict"] != payload["verdict"]
        or isinstance(attestation["final_round"], bool)
        or not isinstance(attestation["final_round"], int)
        or not 0
        <= attestation["final_round"]
        <= limits["max_expansion_rounds"]
        or not isinstance(attestation["max_chars"], int)
        or attestation["max_chars"] != limits["context_max_chars"]
    ):
        raise ValueError("targeted verifier context attestation is inconsistent")
    expanded_ids = attestation["expanded_proof_ids"]
    if (
        not isinstance(expanded_ids, list)
        or len(expanded_ids) > limits["max_expanded_proofs"]
        or len(set(expanded_ids)) != len(expanded_ids)
        or any(not isinstance(value, str) or _ITEM_ID_RE.fullmatch(value) is None for value in expanded_ids)
        or not isinstance(attestation.get("context_digest"), str)
        or _HEX_DIGEST_RE.fullmatch(attestation["context_digest"]) is None
    ):
        raise ValueError("targeted verifier expanded proof ids are invalid")
    if rebuild_with_current_parser:
        assert manifest is not None and item is not None
        rebuilt = build_item_context(
            manifest,
            item.item_id,
            max_chars=attestation["max_chars"],
            expanded_proof_ids=expanded_ids,
            round_index=attestation["final_round"],
        )
        if (
            rebuilt["digest"] != attestation["context_digest"]
            or rebuilt["expanded_proof_ids"] != expanded_ids
            or rebuilt["expanded_proof_characters"]
            > limits["max_expanded_proof_chars"]
            or rebuilt["missing"]
            or rebuilt["omitted"]
        ):
            raise ValueError("targeted verifier context cannot be rebuilt exactly")
    if (
        payload["publication_authority"] is not False
        or payload["whole_blueprint_verdict_authority"] is not False
    ):
        raise ValueError("targeted verifier receipt attempted to acquire publication authority")
    seed = dict(payload)
    receipt_sha = seed.pop("receipt_sha256")
    if not isinstance(receipt_sha, str) or _HEX_DIGEST_RE.fullmatch(receipt_sha) is None:
        raise ValueError("targeted verifier receipt digest is invalid")
    encoded = json.dumps(
        seed,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded) > execution_binding["prompt_limits"][
        "max_targeted_receipt_bytes"
    ]:
        raise ValueError("targeted verifier receipt exceeds its byte bound")
    if hashlib.sha256(encoded).hexdigest() != receipt_sha:
        raise ValueError("targeted verifier receipt content address mismatch")
    return dict(payload)


def _targeted_verification_journal_identity(
    *,
    statement: str,
    proof: str,
    ticket: Mapping[str, Any],
    verification_deadline_utc: str,
    endpoint: str,
) -> tuple[dict[str, Any], str, str]:
    try:
        ticket_bytes = _canonical_json_line_bytes(dict(ticket))
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError("targeted verification ticket is not canonical JSON") from exc
    if len(ticket_bytes) > _MAX_TARGETED_VERIFICATION_INTENT_BYTES:
        raise ValueError("targeted verification ticket exceeds its byte bound")
    service_identity = {
        "schema_version": "rethlas_targeted_verification_attempt_identity_v1",
        "statement_sha256": hashlib.sha256(statement.encode("utf-8")).hexdigest(),
        "proof_sha256": hashlib.sha256(proof.encode("utf-8")).hexdigest(),
        "ticket_sha256": hashlib.sha256(ticket_bytes).hexdigest(),
        "verification_deadline_utc": verification_deadline_utc,
    }
    targeted_attempt_id = "target_" + hashlib.sha256(
        _canonical_json_line_bytes(service_identity)
    ).hexdigest()[:32]
    identity = {
        "identity_schema_version": (
            "rethlas_targeted_verification_journal_identity_v1"
        ),
        **{key: value for key, value in service_identity.items() if key != "schema_version"},
        "targeted_attempt_id": targeted_attempt_id,
        "endpoint": endpoint,
    }
    return (
        identity,
        hashlib.sha256(_canonical_json_line_bytes(identity)).hexdigest(),
        targeted_attempt_id,
    )


def _targeted_verification_journal_names(
    journal_key: str,
) -> tuple[str, str, str, str]:
    if _HEX_DIGEST_RE.fullmatch(journal_key) is None:
        raise ValueError("targeted verification journal key is invalid")
    prefix = f".rethlas-targeted-verification-{journal_key}"
    return (
        prefix + ".lock",
        prefix + ".intent.json",
        prefix + ".dispatch.json",
        prefix + ".result.json",
    )


def _validate_targeted_journal_timestamp(value: object, *, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} timestamp is invalid")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{label} timestamp is invalid") from exc
    if (
        parsed.tzinfo is None
        or parsed.utcoffset() != timedelta(0)
        or value != parsed.astimezone(timezone.utc).isoformat()
    ):
        raise ValueError(f"{label} timestamp is invalid")
    return value


def _read_targeted_verification_journal(
    *,
    directory_fd: int,
    identity: Mapping[str, Any],
    journal_key: str,
    intent_name: str,
    dispatch_name: str,
    result_name: str,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, dict[str, Any] | None]:
    intent = _read_canonical_record_at(
        directory_fd,
        intent_name,
        maximum_bytes=_MAX_TARGETED_VERIFICATION_INTENT_BYTES,
        label="targeted verification intent",
    )
    dispatch = _read_canonical_record_at(
        directory_fd,
        dispatch_name,
        maximum_bytes=_MAX_TARGETED_VERIFICATION_DISPATCH_BYTES,
        label="targeted verification dispatch",
    )
    result = _read_canonical_record_at(
        directory_fd,
        result_name,
        maximum_bytes=_MAX_TARGETED_VERIFICATION_RESULT_BYTES,
        label="targeted verification result",
    )
    if intent is None:
        if dispatch is not None or result is not None:
            raise ValueError("targeted verification effect lacks its intent")
        return None, None, None
    expected_intent = {
        "schema_version": _TARGETED_VERIFICATION_INTENT_SCHEMA,
        "status": "reserved",
        "journal_key": journal_key,
        **dict(identity),
    }
    expected_intent_keys = {
        *expected_intent,
        "created_at_utc",
        "proof_context",
    }
    if (
        set(intent) != expected_intent_keys
        or any(intent.get(key) != value for key, value in expected_intent.items())
    ):
        raise ValueError("targeted verification intent binding mismatch")
    _validate_targeted_journal_timestamp(
        intent.get("created_at_utc"), label="targeted verification intent"
    )
    _validate_proof_context_binding(intent.get("proof_context"))
    intent_sha256 = hashlib.sha256(_canonical_json_line_bytes(intent)).hexdigest()
    if dispatch is None:
        if result is not None:
            raise ValueError("targeted verification result lacks its dispatch")
        return intent, None, None
    if (
        set(dispatch)
        != {
            "schema_version",
            "status",
            "journal_key",
            "intent_sha256",
            "dispatched_at_utc",
        }
        or dispatch.get("schema_version")
        != _TARGETED_VERIFICATION_DISPATCH_SCHEMA
        or dispatch.get("status") != "dispatched"
        or dispatch.get("journal_key") != journal_key
        or dispatch.get("intent_sha256") != intent_sha256
    ):
        raise ValueError("targeted verification dispatch binding mismatch")
    _validate_targeted_journal_timestamp(
        dispatch.get("dispatched_at_utc"), label="targeted verification dispatch"
    )
    if result is None:
        return intent, dispatch, None
    dispatch_sha256 = hashlib.sha256(
        _canonical_json_line_bytes(dispatch)
    ).hexdigest()
    status = result.get("status")
    receipt = result.get("verification_receipt")
    error_sha256 = result.get("error_sha256")
    if (
        set(result)
        != {
            "schema_version",
            "status",
            "journal_key",
            "intent_sha256",
            "dispatch_sha256",
            "verification_receipt",
            "error_sha256",
            "settled_at_utc",
        }
        or result.get("schema_version") != _TARGETED_VERIFICATION_RESULT_SCHEMA
        or status not in {"completed", "operational_blocked"}
        or result.get("journal_key") != journal_key
        or result.get("intent_sha256") != intent_sha256
        or result.get("dispatch_sha256") != dispatch_sha256
        or (
            status == "completed"
            and (not isinstance(receipt, dict) or error_sha256 is not None)
        )
        or (
            status == "operational_blocked"
            and (
                receipt is not None
                or not isinstance(error_sha256, str)
                or _HEX_DIGEST_RE.fullmatch(error_sha256) is None
            )
        )
    ):
        raise ValueError("targeted verification result binding mismatch")
    _validate_targeted_journal_timestamp(
        result.get("settled_at_utc"), label="targeted verification result"
    )
    return intent, dispatch, result


def _commit_targeted_verification_result(
    *,
    directory_fd: int,
    result_name: str,
    journal_key: str,
    intent: Mapping[str, Any],
    dispatch: Mapping[str, Any],
    status: str,
    verification_receipt: Mapping[str, Any] | None,
    error_sha256: str | None,
) -> dict[str, Any]:
    result = {
        "schema_version": _TARGETED_VERIFICATION_RESULT_SCHEMA,
        "status": status,
        "journal_key": journal_key,
        "intent_sha256": hashlib.sha256(
            _canonical_json_line_bytes(dict(intent))
        ).hexdigest(),
        "dispatch_sha256": hashlib.sha256(
            _canonical_json_line_bytes(dict(dispatch))
        ).hexdigest(),
        "verification_receipt": (
            None
            if verification_receipt is None
            else dict(verification_receipt)
        ),
        "error_sha256": error_sha256,
        "settled_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    return _write_once_canonical_record_at(
        directory_fd,
        result_name,
        result,
        maximum_bytes=_MAX_TARGETED_VERIFICATION_RESULT_BYTES,
        label="targeted verification result",
    )


def _targeted_operational_error_sha256(error: BaseException) -> str:
    durable = getattr(error, "error_sha256", None)
    if isinstance(durable, str) and _HEX_DIGEST_RE.fullmatch(durable) is not None:
        return durable
    return hashlib.sha256(
        f"{type(error).__module__}.{type(error).__qualname__}".encode("utf-8")
    ).hexdigest()


def _validate_targeted_status_terminal(
    payload: object,
    *,
    response_status_code: object,
    identity: Mapping[str, Any],
) -> dict[str, Any]:
    fields = {
        "schema_version",
        "targeted_attempt_id",
        "state",
        "status_code",
        "detail",
        "attempt_identity_sha256",
        "intent_sha256",
        "failure_sha256",
        "model_dispatched",
        "terminal_sha256",
    }
    if not isinstance(payload, dict) or set(payload) != {"detail"}:
        raise ValueError("targeted status terminal envelope is unavailable")
    envelope = payload.get("detail")
    if not isinstance(envelope, dict) or set(envelope) != fields:
        raise ValueError("targeted status terminal envelope has an invalid shape")
    status_code = envelope.get("status_code")
    state = envelope.get("state")
    detail = envelope.get("detail")
    if (
        envelope.get("schema_version") != _TARGETED_STATUS_TERMINAL_SCHEMA
        or envelope.get("targeted_attempt_id")
        != identity.get("targeted_attempt_id")
        or state
        not in {"predispatch_failed", "operational_failed", "execution_unknown"}
        or type(status_code) is not int
        or status_code != response_status_code
        or not 400 <= status_code <= 599
        or detail is None
        or type(envelope.get("model_dispatched")) is not bool
        or envelope["model_dispatched"] != (state != "predispatch_failed")
        or any(
            not isinstance(envelope.get(name), str)
            or _HEX_DIGEST_RE.fullmatch(envelope[name]) is None
            for name in (
                "attempt_identity_sha256",
                "intent_sha256",
                "failure_sha256",
                "terminal_sha256",
            )
        )
    ):
        raise ValueError("targeted status terminal envelope binding mismatch")
    service_identity = {
        "schema_version": "rethlas_targeted_verification_attempt_identity_v1",
        "statement_sha256": identity.get("statement_sha256"),
        "proof_sha256": identity.get("proof_sha256"),
        "ticket_sha256": identity.get("ticket_sha256"),
        "verification_deadline_utc": identity.get("verification_deadline_utc"),
    }
    canonical = lambda value: json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if (
        hashlib.sha256(canonical(service_identity)).hexdigest()
        != envelope["attempt_identity_sha256"]
        or hashlib.sha256(
            canonical({"status_code": status_code, "detail": detail})
        ).hexdigest()
        != envelope["failure_sha256"]
    ):
        raise ValueError("targeted status terminal evidence mismatch")
    terminal = dict(envelope)
    terminal_sha256 = terminal.pop("terminal_sha256")
    if hashlib.sha256(canonical(terminal)).hexdigest() != terminal_sha256:
        raise ValueError("targeted status terminal content address mismatch")
    return dict(envelope)


def _validate_targeted_status_pending(
    payload: object,
    *,
    response_status_code: object,
    identity: Mapping[str, Any],
) -> dict[str, Any]:
    fields = {
        "schema_version",
        "targeted_attempt_id",
        "state",
        "attempt_state",
        "attempt_identity_sha256",
        "intent_sha256",
        "proof_context",
        "pending_sha256",
    }
    if response_status_code != 425 or not isinstance(payload, dict) or set(payload) != {"detail"}:
        raise ValueError("targeted pending status is unavailable")
    envelope = payload.get("detail")
    if (
        not isinstance(envelope, dict)
        or set(envelope) != fields
        or envelope.get("schema_version") != _TARGETED_STATUS_PENDING_SCHEMA
        or envelope.get("targeted_attempt_id") != identity.get("targeted_attempt_id")
        or envelope.get("state") != "recover_via_post"
        or envelope.get("attempt_state") not in {"ready", "running"}
        or any(
            not isinstance(envelope.get(name), str)
            or _HEX_DIGEST_RE.fullmatch(envelope[name]) is None
            for name in (
                "attempt_identity_sha256",
                "intent_sha256",
                "pending_sha256",
            )
        )
    ):
        raise ValueError("targeted pending status binding mismatch")
    service_identity = {
        "schema_version": "rethlas_targeted_verification_attempt_identity_v1",
        "statement_sha256": identity.get("statement_sha256"),
        "proof_sha256": identity.get("proof_sha256"),
        "ticket_sha256": identity.get("ticket_sha256"),
        "verification_deadline_utc": identity.get("verification_deadline_utc"),
    }
    canonical = lambda value: json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if hashlib.sha256(canonical(service_identity)).hexdigest() != envelope[
        "attempt_identity_sha256"
    ]:
        raise ValueError("targeted pending status identity mismatch")
    _validate_proof_context_binding(envelope.get("proof_context"))
    seed = dict(envelope)
    pending_sha256 = seed.pop("pending_sha256")
    if hashlib.sha256(canonical(seed)).hexdigest() != pending_sha256:
        raise ValueError("targeted pending status content address mismatch")
    return dict(envelope)


def _targeted_journal_result_or_raise(
    *,
    result: Mapping[str, Any],
    intent: Mapping[str, Any],
    ticket: Dict[str, Any],
    statement: str,
    proof: str,
    verification_deadline_utc: str,
) -> Dict[str, Any]:
    if result.get("status") == "operational_blocked":
        raise TargetedVerificationOperationalBlocked(
            str(result["error_sha256"]),
            "targeted verification replayed a concrete operational failure",
        )
    return validate_targeted_claim_receipt(
        result.get("verification_receipt"),
        ticket=ticket,
        statement=statement,
        proof=proof,
        verification_deadline_utc=verification_deadline_utc,
        expected_proof_context=intent["proof_context"],
    )


def verify_targeted_claim_service(
    *,
    statement: str,
    proof: str,
    ticket: Dict[str, Any],
    verification_deadline_utc: str,
    endpoint: str,
    timeout_seconds: int = 3600,
    api_token: str | None = None,
    journal_root: Path | None = None,
    on_verifier_dispatch: Callable[[], None] | None = None,
    recover_only: bool = False,
) -> Dict[str, Any]:
    """Invoke the isolated service once for one prevalidated official ticket."""

    if not isinstance(statement, str) or not statement.strip():
        raise ValueError("statement must be non-empty")
    if not isinstance(proof, str) or not proof.strip():
        raise ValueError("blueprint must be non-empty")
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, int)
        or timeout_seconds <= 0
    ):
        raise ValueError("timeout_seconds must be > 0")
    if on_verifier_dispatch is not None and not callable(on_verifier_dispatch):
        raise ValueError("on_verifier_dispatch must be callable")
    if not isinstance(recover_only, bool):
        raise ValueError("recover_only must be a boolean")
    try:
        deadline = datetime.fromisoformat(verification_deadline_utc)
    except (TypeError, ValueError) as exc:
        raise ValueError("verification_deadline_utc is invalid") from exc
    if (
        deadline.tzinfo is None
        or deadline.utcoffset() != timedelta(0)
        or verification_deadline_utc
        != deadline.astimezone(timezone.utc).isoformat()
    ):
        raise ValueError("verification_deadline_utc must be canonical UTC")
    endpoint = _validate_endpoint(endpoint)
    identity, journal_key, targeted_attempt_id = (
        _targeted_verification_journal_identity(
            statement=statement,
            proof=proof,
            ticket=ticket,
            verification_deadline_utc=verification_deadline_utc,
            endpoint=endpoint,
        )
    )
    request_kwargs: Dict[str, Any] = {
        "json": {
            "statement": statement,
            "proof": proof,
            "ticket": ticket,
            "verification_deadline_utc": verification_deadline_utc,
            "targeted_attempt_id": targeted_attempt_id,
        },
        "timeout": timeout_seconds,
    }
    if api_token:
        request_kwargs["headers"] = {"Authorization": f"Bearer {api_token}"}

    def invoke_service(
        expected_proof_context: Mapping[str, Any]
    ) -> Dict[str, Any]:
        response = requests.post(endpoint, **request_kwargs)
        if getattr(response, "status_code", None) != 200:
            # A POST response can be synthesized after the body was forwarded.
            # Only the authenticated read-only status endpoint may settle a
            # remote failure or execution-unknown outcome.
            raise TargetedVerificationLocalRetryable(
                "targeted verification POST requires authenticated status recovery"
            )
        try:
            raw_payload = response.json()
        except ValueError as exc:
            raise ValueError(
                "targeted verification service returned non-JSON"
            ) from exc
        return validate_targeted_claim_receipt(
            raw_payload,
            ticket=ticket,
            statement=statement,
            proof=proof,
            verification_deadline_utc=verification_deadline_utc,
            expected_proof_context=expected_proof_context,
        )

    def lookup_status(
        expected_proof_context: Mapping[str, Any] | None,
    ) -> tuple[str, Dict[str, Any] | None]:
        status_kwargs: Dict[str, Any] = {"timeout": timeout_seconds}
        if api_token:
            status_kwargs["headers"] = {"Authorization": f"Bearer {api_token}"}
        try:
            status_response = requests.get(
                _targeted_status_endpoint(endpoint, targeted_attempt_id),
                **status_kwargs,
            )
        except requests.RequestException as exc:
            raise TargetedVerificationLocalRetryable(
                "targeted verification status lookup is retryable"
            ) from exc
        status_code = getattr(status_response, "status_code", None)
        if type(status_code) is not int:
            raise TargetedVerificationLocalRetryable(
                "targeted verification status omitted its HTTP status"
            )
        try:
            status_payload = status_response.json()
        except ValueError as exc:
            raise TargetedVerificationLocalRetryable(
                "targeted verification status returned unusable JSON"
            ) from exc
        if (
            status_code == 404
            and isinstance(status_payload, dict)
            and set(status_payload) == {"detail"}
            and isinstance(status_payload.get("detail"), dict)
            and set(status_payload["detail"])
            == {"code", "targeted_attempt_id"}
            and status_payload["detail"].get("code")
            == "targeted_attempt_not_found"
            and status_payload["detail"].get("targeted_attempt_id")
            == targeted_attempt_id
        ):
            return "missing", None
        if status_code == 200:
            try:
                binding = (
                    _validate_proof_context_binding(
                        status_payload.get("proof_context")
                    )
                    if expected_proof_context is None
                    and isinstance(status_payload, dict)
                    else expected_proof_context
                )
                if binding is None:
                    raise ValueError("targeted status omitted proof-context binding")
                return (
                    "receipt",
                    validate_targeted_claim_receipt(
                        status_payload,
                        ticket=ticket,
                        statement=statement,
                        proof=proof,
                        verification_deadline_utc=verification_deadline_utc,
                        expected_proof_context=binding,
                    ),
                )
            except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
                raise TargetedVerificationLocalRetryable(
                    "targeted verification status receipt is retryable"
                ) from exc
        if status_code == 425:
            try:
                pending = _validate_targeted_status_pending(
                    status_payload,
                    response_status_code=status_code,
                    identity=identity,
                )
                pending_proof_context = _validate_proof_context_binding(
                    pending["proof_context"]
                )
                if (
                    expected_proof_context is not None
                    and pending_proof_context != expected_proof_context
                ):
                    raise ValueError(
                        "targeted pending status proof-context binding mismatch"
                    )
            except (TypeError, ValueError, RecursionError) as exc:
                raise TargetedVerificationLocalRetryable(
                    "targeted verification pending status is unauthenticated"
                ) from exc
            return "recover_via_post", pending
        try:
            terminal = _validate_targeted_status_terminal(
                status_payload,
                response_status_code=status_code,
                identity=identity,
            )
        except (TypeError, ValueError, RecursionError) as exc:
            raise TargetedVerificationLocalRetryable(
                "targeted verification status failure is unauthenticated"
            ) from exc
        if terminal["state"] == "execution_unknown":
            raise VerificationExecutionUnknown(
                "targeted verification has a durable unknown terminal"
            )
        raise _TargetedVerificationDurableRemoteFailure(
            str(terminal["failure_sha256"])
        )

    if journal_root is None:
        if recover_only:
            raise ValueError("recover_only requires a targeted journal root")
        if on_verifier_dispatch is not None:
            raise ValueError(
                "on_verifier_dispatch requires a targeted journal root"
            )
        return invoke_service(_current_proof_context_binding())

    journal_root = _absolute_path(Path(journal_root))
    lock_name, intent_name, dispatch_name, result_name = (
        _targeted_verification_journal_names(journal_key)
    )
    directory_fd = _open_or_create_directory_durable(
        journal_root, label="targeted verification journal root"
    )

    def assert_journal_bound() -> None:
        try:
            _assert_directory_binding(
                journal_root,
                directory_fd,
                label="targeted verification journal root",
            )
        except ValueError as exc:
            raise TargetedVerificationLocalRetryable(
                "targeted verification journal root moved"
            ) from exc

    lock_handle: Any | None = None
    try:
        assert_journal_bound()
        lock_handle = _open_lock_file_at(
            directory_fd,
            lock_name,
            display_path=journal_root / lock_name,
        )
        _acquire_publication_lock(
            lock_handle, display_path=journal_root / lock_name
        )
        assert_journal_bound()
        intent, dispatch, result = _read_targeted_verification_journal(
            directory_fd=directory_fd,
            identity=identity,
            journal_key=journal_key,
            intent_name=intent_name,
            dispatch_name=dispatch_name,
            result_name=result_name,
        )
        precovered_receipt: Dict[str, Any] | None = None
        prechecked_remote_missing = False
        prechecked_remote_recovery: Dict[str, Any] | None = None
        if intent is None and recover_only:
            status_kind, status_payload = lookup_status(None)
            if status_kind == "receipt":
                precovered_receipt = status_payload
            elif status_kind == "recover_via_post":
                prechecked_remote_recovery = status_payload
            else:
                prechecked_remote_missing = True
        if result is not None:
            return _targeted_journal_result_or_raise(
                result=result,
                intent=intent,
                ticket=ticket,
                statement=statement,
                proof=proof,
                verification_deadline_utc=verification_deadline_utc,
            )
        # A missing local dispatch can mean that the journal directory was
        # rotated after the content-addressed remote attempt completed.  In
        # recovery mode, reconstruct the local evidence and query status before
        # deciding whether the one safe POST is still needed.
        recovering_dispatch = dispatch is not None or recover_only
        if intent is None:
            proof_context_binding = (
                _validate_proof_context_binding(
                    precovered_receipt["proof_context"]
                )
                if precovered_receipt is not None
                else (
                    _validate_proof_context_binding(
                        prechecked_remote_recovery["proof_context"]
                    )
                    if prechecked_remote_recovery is not None
                    else _current_proof_context_binding()
                )
            )
            intent = {
                "schema_version": _TARGETED_VERIFICATION_INTENT_SCHEMA,
                "status": "reserved",
                "journal_key": journal_key,
                **identity,
                "created_at_utc": datetime.now(timezone.utc).isoformat(),
                "proof_context": proof_context_binding,
            }
            intent = _write_once_canonical_record_at(
                directory_fd,
                intent_name,
                intent,
                maximum_bytes=_MAX_TARGETED_VERIFICATION_INTENT_BYTES,
                label="targeted verification intent",
            )
        if dispatch is None:
            assert_journal_bound()
            dispatch = {
                "schema_version": _TARGETED_VERIFICATION_DISPATCH_SCHEMA,
                "status": "dispatched",
                "journal_key": journal_key,
                "intent_sha256": hashlib.sha256(
                    _canonical_json_line_bytes(intent)
                ).hexdigest(),
                "dispatched_at_utc": datetime.now(timezone.utc).isoformat(),
            }
            dispatch = _write_once_canonical_record_at(
                directory_fd,
                dispatch_name,
                dispatch,
                maximum_bytes=_MAX_TARGETED_VERIFICATION_DISPATCH_BYTES,
                label="targeted verification dispatch",
            )

        if on_verifier_dispatch is not None:
            try:
                on_verifier_dispatch()
            except Exception as exc:
                raise TargetedVerificationLocalRetryable(
                    "targeted verification local dispatch transition is retryable"
                ) from exc
        try:
            receipt = precovered_receipt
            recover_via_post = prechecked_remote_recovery is not None
            if (
                recovering_dispatch
                and receipt is None
                and not prechecked_remote_missing
                and not recover_via_post
            ):
                status_kind, status_payload = lookup_status(
                    intent["proof_context"]
                )
                if status_kind == "receipt":
                    receipt = status_payload
                elif status_kind == "recover_via_post":
                    recover_via_post = True
            if receipt is None:
                if not recover_via_post and datetime.now(timezone.utc) >= deadline:
                    raise TimeoutError(
                        "targeted verification deadline expired before remote admission"
                    )
                try:
                    receipt = invoke_service(intent["proof_context"])
                except requests.RequestException as exc:
                    # A POST response is not itself durable attempt evidence:
                    # gateways can synthesize any status after forwarding the
                    # body.  Only the read-only status endpoint's bound receipt
                    # or content-addressed terminal may settle this journal.
                    raise TargetedVerificationLocalRetryable(
                        "targeted verification POST requires status recovery"
                    ) from exc
                except ValueError as exc:
                    # A proxy-truncated or otherwise unusable POST response can
                    # still correspond to a completed durable remote receipt.
                    raise TargetedVerificationLocalRetryable(
                        "targeted verification POST response requires status recovery"
                    ) from exc
            assert_journal_bound()
        except Exception as exc:
            if bool(getattr(exc, "local_retryable", False)):
                raise
            if isinstance(exc, VerificationExecutionUnknown):
                raise TargetedVerificationExecutionUnknown(
                    "targeted verification outcome is unknown after dispatch"
                ) from exc
            if (
                isinstance(exc, requests.RequestException)
                and getattr(exc, "response", None) is None
            ):
                raise TargetedVerificationLocalRetryable(
                    "targeted verification transport requires status recovery"
                ) from exc
            error_sha256 = _targeted_operational_error_sha256(exc)
            _commit_targeted_verification_result(
                directory_fd=directory_fd,
                result_name=result_name,
                journal_key=journal_key,
                intent=intent,
                dispatch=dispatch,
                status="operational_blocked",
                verification_receipt=None,
                error_sha256=error_sha256,
            )
            raise TargetedVerificationOperationalBlocked(
                error_sha256,
                "targeted verification reached a concrete operational failure",
            ) from exc

        try:
            _commit_targeted_verification_result(
                directory_fd=directory_fd,
                result_name=result_name,
                journal_key=journal_key,
                intent=intent,
                dispatch=dispatch,
                status="completed",
                verification_receipt=receipt,
                error_sha256=None,
            )
        except OSError as exc:
            # The remote content-addressed receipt remains recoverable through
            # status; a local durability fault must not become a host terminal.
            raise TargetedVerificationLocalRetryable(
                "targeted verification local result commit is retryable"
            ) from exc
        return receipt
    except OSError as exc:
        raise TargetedVerificationLocalRetryable(
            "targeted verification local journal operation is retryable"
        ) from exc
    finally:
        if lock_handle is not None:
            try:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
            finally:
                lock_handle.close()
        os.close(directory_fd)


def verify_blueprint_file(
    *,
    statement: str,
    draft_path: Path,
    verified_path: Path,
    endpoint: str,
    verification_deadline_utc: str | None = None,
    timeout_seconds: int = 3600,
    api_token: str | None = None,
    receipt_path: Path | None = None,
    problem_id: str | None = None,
    blueprint_root: Path | None = None,
    publication_state_root: Path | None = None,
    verification_quorum: int = 2,
    supersedes: list[dict[str, str]] | None = None,
    verification_profile: str | None = None,
    on_verifier_dispatch: Callable[[], None] | None = None,
    prepared_only: bool = False,
    publication_authority_intent_sha256: str | None = None,
    on_publication_admission_recovery: (
        Callable[[Mapping[str, Any]], Mapping[str, Any]] | None
    ) = None,
    resume_dispatched: bool = False,
) -> Dict[str, Any]:
    """Verify a draft and promote it only if its content is still unchanged."""

    if not isinstance(statement, str) or not statement.strip():
        raise ValueError("statement must be non-empty")
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be > 0")
    if isinstance(verification_quorum, bool) or verification_quorum != 2:
        raise ValueError("whole-proof publication requires verification_quorum=2")
    if on_verifier_dispatch is not None and not callable(on_verifier_dispatch):
        raise ValueError("on_verifier_dispatch must be callable")
    if not isinstance(prepared_only, bool):
        raise ValueError("prepared_only must be a boolean")
    if not isinstance(resume_dispatched, bool):
        raise ValueError("resume_dispatched must be a boolean")
    if prepared_only and resume_dispatched:
        raise ValueError("prepared_only and resume_dispatched are mutually exclusive")
    if (
        publication_authority_intent_sha256 is not None
        and (
            not isinstance(publication_authority_intent_sha256, str)
            or _HEX_DIGEST_RE.fullmatch(
                publication_authority_intent_sha256
            )
            is None
        )
    ):
        raise ValueError("publication authority intent digest is invalid")
    if (
        on_publication_admission_recovery is not None
        and not callable(on_publication_admission_recovery)
    ):
        raise ValueError("publication admission recovery hook must be callable")
    if supersedes is None:
        supersedes = []
    if not isinstance(supersedes, list) or len(supersedes) > 1:
        raise ValueError("supersedes must contain at most one publication")
    for entry in supersedes:
        if (
            not isinstance(entry, dict)
            or set(entry) != {"problem_id", "receipt_sha256", "proof_digest"}
            or not isinstance(entry["problem_id"], str)
            or not entry["problem_id"]
            or _HEX_DIGEST_RE.fullmatch(entry["receipt_sha256"]) is None
            or _HEX_DIGEST_RE.fullmatch(entry["proof_digest"]) is None
        ):
            raise ValueError("supersedes contains an invalid publication binding")
    if verification_deadline_utc is None:
        verification_deadline_utc = (
            datetime.now(timezone.utc) + timedelta(seconds=timeout_seconds)
        ).isoformat()
    try:
        deadline = datetime.fromisoformat(verification_deadline_utc)
    except (TypeError, ValueError) as exc:
        raise ValueError("verification_deadline_utc is invalid") from exc
    if (
        deadline.tzinfo is None
        or deadline.utcoffset() != timedelta(0)
        or verification_deadline_utc
        != deadline.astimezone(timezone.utc).isoformat()
    ):
        raise ValueError("verification_deadline_utc must be canonical UTC")
    if (
        receipt_path is None
        and (deadline - datetime.now(timezone.utc)).total_seconds() <= 0
    ):
        # Receipt-less calls have no durable publication evidence to recover.
        raise ValueError("verification_deadline_utc has already expired")
    endpoint = _validate_endpoint(endpoint)
    if verification_profile is None:
        verification_profile = os.getenv(
            "RETHLAS_MODEL_POLICY_PROFILE", "compatible"
        )
    if verification_profile not in {
        "compatible",
        "balanced",
        "economy",
        "max_diversity",
    }:
        raise ValueError("verification_profile is unsupported")
    draft_path = _absolute_path(draft_path)
    verified_path = _absolute_path(verified_path)
    if blueprint_root is not None:
        blueprint_root = _absolute_path(blueprint_root)
    if publication_state_root is not None:
        publication_state_root = _absolute_path(publication_state_root)
    if receipt_path is not None:
        receipt_path = _absolute_path(receipt_path)
    if draft_path == verified_path:
        raise ValueError("draft and verified paths must be different")
    if receipt_path is not None and not problem_id:
        raise ValueError("problem_id is required when writing a receipt")
    if receipt_path in {draft_path, verified_path}:
        raise ValueError("receipt path must be different from blueprint paths")

    # Open both parent directories before reading or making the network call.
    # Production additionally binds them component-by-component beneath a held
    # results-root descriptor, closing containment-check-to-open symlink races.
    blueprint_root_fd = -1
    draft_parent_fd = -1
    verified_parent_fd = -1
    draft_parent_parts: tuple[str, ...] = ()
    verified_parent_parts: tuple[str, ...] = ()
    physical_turn_request_sha256: str | None = None
    verifier_dispatch_committed = False
    verification_effect_lock_handle: Any | None = None
    publication_identity_lock_handle: Any | None = None
    prepared_receipt_parent_fd = -1
    direct_journal_parent: Path | None = None
    direct_journal_parent_fd = -1
    direct_finalization_state: tuple[
        dict[str, Any], Path, Path
    ] | None = None
    replaceable_settled_receipt: tuple[dict[str, Any], bytes] | None = None
    prepared_archive_identity: dict[str, Any] | None = None
    archived_prepared: tuple[dict[str, Any], bytes] | None = None
    publication_admission_identity: dict[str, Any] | None = None
    observed_publication_admission: dict[str, Any] | None = None
    current_publication_admission: dict[str, Any] | None = None
    admission_effect_intent_sha256: str | None = None
    publication_verifier_binding: dict[str, Any] | None = None

    def assert_blueprint_bindings() -> None:
        if blueprint_root is not None:
            assert blueprint_root_fd >= 0
            _assert_directory_binding(
                blueprint_root,
                blueprint_root_fd,
                label="trusted blueprint root",
            )
            _assert_directory_at_binding(
                blueprint_root_fd,
                draft_parent_parts,
                draft_parent_fd,
                label="blueprint draft parent",
            )
            _assert_directory_at_binding(
                blueprint_root_fd,
                verified_parent_parts,
                verified_parent_fd,
                label="verified blueprint parent",
            )
            return
        _assert_directory_binding(
            draft_path.parent,
            draft_parent_fd,
            label="blueprint draft parent",
        )
        _assert_directory_binding(
            verified_path.parent,
            verified_parent_fd,
            label="verified blueprint parent",
        )

    def settle_publication_admission(
        settlement: Mapping[str, Any],
    ) -> None:
        if receipt_path is None or direct_journal_parent is None:
            return
        admission = _read_publication_admission(
            state_parent=direct_journal_parent,
            state_parent_fd=direct_journal_parent_fd,
            receipt_path=receipt_path,
        )
        if admission is None:
            # Upgrade compatibility: historical prepared evidence predates
            # receipt-level admission.  Its settlement is still authoritative;
            # the next fresh generation will create the first admission.
            return
        _settle_publication_admission(
            state_parent=direct_journal_parent,
            state_parent_fd=direct_journal_parent_fd,
            receipt_path=receipt_path,
            reason=str(settlement["reason"]),
            receipt_sha256=str(settlement["receipt_sha256"]),
        )

    def direct_nonpublication_settlement_reason(
        result_value: Mapping[str, Any],
    ) -> str:
        if result_value.get("publication_blocked_reason") in {
            "invalid_verifier_response",
            "operational_verifier_failure",
            "verifier_quorum_not_independent",
        }:
            return "direct_operational_nonpublication"
        return "direct_mathematical_rejection"

    def settle_direct_nonpublication_admission(
        result_value: Mapping[str, Any],
    ) -> None:
        if receipt_path is None or direct_journal_parent is None:
            return
        admission = _read_publication_admission(
            state_parent=direct_journal_parent,
            state_parent_fd=direct_journal_parent_fd,
            receipt_path=receipt_path,
        )
        if admission is None:
            return
        _settle_publication_admission(
            state_parent=direct_journal_parent,
            state_parent_fd=direct_journal_parent_fd,
            receipt_path=receipt_path,
            reason=direct_nonpublication_settlement_reason(result_value),
            receipt_sha256=None,
        )

    def ensure_prepared_settlement_mirrored(
        *,
        identity: Mapping[str, Any],
        settlement: Mapping[str, Any],
    ) -> None:
        nonlocal prepared_receipt_parent_fd
        if receipt_path is None or direct_journal_parent is None:
            return
        if prepared_receipt_parent_fd < 0:
            prepared_receipt_parent_fd = _open_or_create_directory_durable(
                receipt_path.parent,
                label="prepared receipt parent",
            )
        observed = _read_prepared_publication_settlement(
            receipt_path=receipt_path,
            identity=identity,
            state_parent_fd=direct_journal_parent_fd,
            receipt_parent_fd=prepared_receipt_parent_fd,
        )
        if observed != settlement:
            raise ValueError("prepared publication settlement mirror mismatch")

    try:
        if blueprint_root is None:
            verified_path.parent.mkdir(parents=True, exist_ok=True)
            draft_parent_fd = _open_directory(
                draft_path.parent,
                label="blueprint draft parent",
            )
            verified_parent_fd = _open_directory(
                verified_path.parent,
                label="verified blueprint parent",
            )
        else:
            draft_parent_parts = _directory_parts_beneath(
                blueprint_root,
                draft_path.parent,
                label="blueprint draft parent",
            )
            verified_parent_parts = _directory_parts_beneath(
                blueprint_root,
                verified_path.parent,
                label="verified blueprint parent",
            )
            blueprint_root_fd = _open_directory(
                blueprint_root,
                label="trusted blueprint root",
            )
            draft_parent_fd = _open_directory_at(
                blueprint_root_fd,
                draft_parent_parts,
                label="blueprint draft parent",
            )
            verified_parent_fd = _open_directory_at(
                blueprint_root_fd,
                verified_parent_parts,
                label="verified blueprint parent",
            )
        assert_blueprint_bindings()

        if receipt_path is not None:
            if publication_state_root is not None:
                direct_journal_parent = publication_state_root
            elif blueprint_root is not None:
                direct_journal_parent = _direct_finalization_journal_parent(
                    receipt_path=receipt_path,
                    verified_path=verified_path,
                    blueprint_root=blueprint_root,
                )
            else:
                # Backward-compatible callers have no declared trust root.  At
                # minimum choose a receipt-only location, never a target-
                # dependent common ancestor.  Production callers supply either
                # blueprint_root or publication_state_root explicitly.
                direct_journal_parent = receipt_path.parent.parent
                if direct_journal_parent == Path(
                    direct_journal_parent.anchor
                ):
                    raise ValueError(
                        "publication receipt lacks a trusted state root"
                    )
            direct_journal_parent_fd = _open_or_create_directory_durable(
                direct_journal_parent,
                label="direct finalization journal parent",
            )
            _assert_directory_binding(
                direct_journal_parent,
                direct_journal_parent_fd,
                label="direct finalization journal parent",
            )
            publication_identity_lock_name = _publication_identity_lock_name(
                receipt_path=receipt_path,
            )
            publication_identity_lock_path = (
                direct_journal_parent / publication_identity_lock_name
            )
            publication_identity_lock_handle = _open_lock_file_at(
                direct_journal_parent_fd,
                publication_identity_lock_name,
                display_path=publication_identity_lock_path,
            )
            _acquire_publication_lock(
                publication_identity_lock_handle,
                display_path=publication_identity_lock_path,
            )

        proof = _read_regular_blueprint_at(
            draft_parent_fd,
            draft_path.name,
            display_path=draft_path,
            label="blueprint draft",
            maximum_bytes=ABSOLUTE_MAX_BLUEPRINT_BYTES,
            maximum_chars=ABSOLUTE_MAX_BLUEPRINT_CHARS,
        )
        if not proof.strip():
            raise ValueError("blueprint draft must be non-empty")
        proof_bytes = proof.encode("utf-8")
        expected_digest = proof_digest(proof)
        expected_manifest = parse_blueprint(proof, target_statement=statement)
        expected_ids = list(expected_manifest.item_ids)
        if len(expected_ids) > ABSOLUTE_MAX_PUBLICATION_PROOF_ITEMS:
            raise ValueError("blueprint exceeds the publication proof-item limit")
        expected_context_digest = aggregate_context_digest(expected_manifest)
        if receipt_path is not None:
            assert direct_journal_parent is not None
            prepared_archive_identity = _prepared_publication_archive_identity(
                problem_id=str(problem_id),
                statement=statement,
                proof_sha256=expected_digest,
                context_digest=expected_context_digest,
                proof_context_source_sha256=(
                    _assert_publication_proof_context_unchanged()
                ),
                supersedes=supersedes,
                verified_path=verified_path,
            )
            publication_admission_identity = _publication_admission_identity(
                receipt_path=receipt_path,
                archive_identity=prepared_archive_identity,
                client_source_sha256=(
                    _assert_verification_client_source_unchanged()
                ),
                endpoint=endpoint,
                verification_profile=verification_profile,
                verification_quorum=verification_quorum,
            )
            observed_publication_admission = _read_publication_admission(
                state_parent=direct_journal_parent,
                state_parent_fd=direct_journal_parent_fd,
                receipt_path=receipt_path,
            )
            if (
                observed_publication_admission is not None
                and observed_publication_admission["status"] == "submitted"
                and observed_publication_admission["phase"] != "reserved"
                and any(
                    observed_publication_admission.get(field)
                    != publication_admission_identity.get(field)
                    for field in _PUBLICATION_ADMISSION_TARGET_FIELDS
                )
            ):
                return _publication_target_collision_result(
                    verification_passes=[]
                )
        lock_name = f".{verified_path.name}.lock"
        lock_path = verified_path.parent / lock_name
        verification_lock_name = f".{verified_path.name}.verification.lock"
        verification_lock_path = verified_path.parent / verification_lock_name
        verification_effect_lock_handle = _open_lock_file_at(
            verified_parent_fd,
            verification_lock_name,
            display_path=verification_lock_path,
        )
        _acquire_publication_lock(
            verification_effect_lock_handle,
            display_path=verification_lock_path,
        )
        if receipt_path is not None:
            assert direct_journal_parent is not None
            assert prepared_archive_identity is not None
            rollback_intent = _read_receipt_collision_rollback(
                state_parent=direct_journal_parent,
                state_parent_fd=direct_journal_parent_fd,
                receipt_path=receipt_path,
            )
            rollback_matches_current_generation = (
                rollback_intent is not None
                and all(
                    rollback_intent.get(field)
                    == prepared_archive_identity.get(field)
                    for field in _PREPARED_PUBLICATION_ARCHIVE_IDENTITY_FIELDS
                )
            )
            if (
                rollback_intent is not None
                and rollback_intent["verified_path"] == str(verified_path)
                and (
                    rollback_intent["status"] == "rollback_required"
                    or rollback_matches_current_generation
                )
            ):
                rollback_archive_identity = {
                    field: rollback_intent[field]
                    for field in _PREPARED_PUBLICATION_ARCHIVE_IDENTITY_FIELDS
                }
                rollback_archive = _read_prepared_publication_archive(
                    state_parent=direct_journal_parent,
                    state_parent_fd=direct_journal_parent_fd,
                    identity=rollback_archive_identity,
                )
                if rollback_archive is None:
                    raise ValueError(
                        "receipt collision rollback lacks prepared archive"
                    )
                rollback_receipt, rollback_receipt_bytes = rollback_archive
                if hashlib.sha256(rollback_receipt_bytes).hexdigest() != (
                    rollback_intent["receipt_sha256"]
                ):
                    raise ValueError(
                        "receipt collision rollback archive mismatch"
                    )
                rollback_identity = _prepared_publication_receipt_identity(
                    rollback_receipt,
                    receipt_bytes=rollback_receipt_bytes,
                    verified_path=verified_path,
                    problem_id=str(rollback_intent["problem_id"]),
                )
                existing_rollback_settlement = (
                    _read_prepared_publication_settlement(
                        receipt_path=receipt_path,
                        identity=rollback_identity,
                        state_parent_fd=direct_journal_parent_fd,
                    )
                )
                if existing_rollback_settlement is not None:
                    ensure_prepared_settlement_mirrored(
                        identity=rollback_identity,
                        settlement=existing_rollback_settlement,
                    )
                    _settle_receipt_collision_rollback(
                        state_parent=direct_journal_parent,
                        state_parent_fd=direct_journal_parent_fd,
                        receipt_path=receipt_path,
                        settlement=existing_rollback_settlement,
                    )
                    settle_publication_admission(
                        existing_rollback_settlement
                    )
                    return _prepared_publication_nonpublication_result(
                        existing_rollback_settlement
                    )
                observed_after_rollback = (
                    _recover_receipt_collision_rollback_at(
                        verified_parent_fd,
                        verified_path.name,
                        rollback=rollback_intent,
                        display_path=verified_path,
                    )
                )
                rollback_settlement = _commit_prepared_publication_settlement(
                    receipt_path=receipt_path,
                    identity=rollback_identity,
                    reason="prepared_target_collision",
                    observed_target_precondition=observed_after_rollback,
                    state_parent_fd=direct_journal_parent_fd,
                )
                ensure_prepared_settlement_mirrored(
                    identity=rollback_identity,
                    settlement=rollback_settlement,
                )
                _settle_receipt_collision_rollback(
                    state_parent=direct_journal_parent,
                    state_parent_fd=direct_journal_parent_fd,
                    receipt_path=receipt_path,
                    settlement=rollback_settlement,
                )
                settle_publication_admission(rollback_settlement)
                return _prepared_publication_nonpublication_result(
                    rollback_settlement
                )
            archived_prepared = _read_prepared_publication_archive(
                state_parent=direct_journal_parent,
                state_parent_fd=direct_journal_parent_fd,
                identity=prepared_archive_identity,
            )
            if archived_prepared is not None:
                archived_receipt, archived_receipt_bytes = archived_prepared
                archived_identity = _prepared_publication_receipt_identity(
                    archived_receipt,
                    receipt_bytes=archived_receipt_bytes,
                    verified_path=verified_path,
                    problem_id=str(problem_id),
                )
                archived_settlement = _read_prepared_publication_settlement(
                    receipt_path=receipt_path,
                    identity=archived_identity,
                    state_parent_fd=direct_journal_parent_fd,
                )
                if archived_settlement is not None and all(
                    archived_identity.get(field)
                    == prepared_archive_identity.get(field)
                    for field in _PREPARED_PUBLICATION_ARCHIVE_IDENTITY_FIELDS
                ):
                    ensure_prepared_settlement_mirrored(
                        identity=archived_identity,
                        settlement=archived_settlement,
                    )
                    settle_publication_admission(archived_settlement)
                    return _prepared_publication_nonpublication_result(
                        archived_settlement
                    )
            try:
                prepared_receipt_parent_fd = _open_directory(
                    receipt_path.parent,
                    label="prepared receipt parent",
                )
            except ValueError:
                try:
                    receipt_path.parent.lstat()
                except FileNotFoundError:
                    prepared = None
                else:
                    raise
            else:
                prepared = _read_canonical_publication_receipt_at(
                    prepared_receipt_parent_fd, receipt_path
                )
                _assert_directory_binding(
                    receipt_path.parent,
                    prepared_receipt_parent_fd,
                    label="prepared receipt parent",
                )
                if prepared is None and archived_prepared is None:
                    os.close(prepared_receipt_parent_fd)
                    prepared_receipt_parent_fd = -1
            if prepared is None and archived_prepared is not None:
                if prepared_receipt_parent_fd < 0:
                    prepared_receipt_parent_fd = (
                        _open_or_create_directory_durable(
                            receipt_path.parent,
                            label="prepared receipt parent",
                        )
                    )
                archived_receipt, _archived_receipt_bytes = archived_prepared
                competing = _read_canonical_publication_receipt_at(
                    prepared_receipt_parent_fd, receipt_path
                )
                if competing is None:
                    _write_receipt_atomic_at(
                        prepared_receipt_parent_fd,
                        receipt_path,
                        archived_receipt,
                        maximum_bytes=(
                            _persisted_publication_receipt_max_bytes(
                                archived_receipt
                            )
                        ),
                    )
                elif competing != archived_prepared:
                    raise ValueError(
                        "canonical receipt conflicts with prepared publication archive"
                    )
                _assert_directory_binding(
                    receipt_path.parent,
                    prepared_receipt_parent_fd,
                    label="prepared receipt parent",
                )
                prepared = archived_prepared
            elif (
                prepared is not None
                and archived_prepared is not None
                and prepared != archived_prepared
            ):
                raise ValueError(
                    "canonical receipt conflicts with prepared publication archive"
                )
            if prepared is not None:
                prepared_receipt, prepared_receipt_bytes = prepared
                (
                    prepared_max_blueprint_bytes,
                    prepared_max_blueprint_chars,
                ) = _persisted_publication_blueprint_limits(
                    prepared_receipt
                )
                try:
                    prepared_identity = _prepared_publication_receipt_identity(
                        prepared_receipt,
                        receipt_bytes=prepared_receipt_bytes,
                        verified_path=verified_path,
                        problem_id=str(problem_id),
                    )
                except ValueError:
                    competing_verified_path = prepared_receipt.get(
                        "verified_path"
                    )
                    competing_problem_id = prepared_receipt.get("problem_id")
                    if (
                        isinstance(competing_verified_path, str)
                        and competing_verified_path != str(verified_path)
                        and isinstance(competing_problem_id, str)
                        and bool(competing_problem_id)
                    ):
                        competing_path = Path(competing_verified_path)
                        if not competing_path.is_absolute():
                            raise
                        _prepared_publication_receipt_identity(
                            prepared_receipt,
                            receipt_bytes=prepared_receipt_bytes,
                            verified_path=competing_path,
                            problem_id=competing_problem_id,
                        )
                        return _publication_target_collision_result(
                            verification_passes=[]
                        )
                    raise
                prepared_settlement = _read_prepared_publication_settlement(
                    receipt_path=receipt_path,
                    identity=prepared_identity,
                    state_parent_fd=direct_journal_parent_fd,
                    receipt_parent_fd=prepared_receipt_parent_fd,
                )
                current_request_matches_prepared = (
                    prepared_identity["statement_source_digest"]
                    == proof_digest(statement)
                    and prepared_identity["canonical_target_digest"]
                    == proof_digest(extract_verification_target(statement))
                    and prepared_identity["proof_digest"] == expected_digest
                    and prepared_identity["context_digest"]
                    == expected_context_digest
                    and prepared_identity["proof_context_source_sha256"]
                    == _assert_publication_proof_context_unchanged()
                    and prepared_identity["supersedes"] == supersedes
                )
                if prepared_settlement is not None:
                    settle_publication_admission(prepared_settlement)
                    if current_request_matches_prepared:
                        return _prepared_publication_nonpublication_result(
                            prepared_settlement
                        )
                    # A write-once settlement makes this old receipt rejected
                    # evidence.  A semantically different request may replace
                    # it only after completing its own verifier quorum.
                    replaceable_settled_receipt = prepared
                elif not current_request_matches_prepared:
                    with _open_lock_file_at(
                        verified_parent_fd,
                        lock_name,
                        display_path=lock_path,
                    ) as lock_handle:
                        _acquire_publication_lock(
                            lock_handle, display_path=lock_path
                        )
                        try:
                            assert_blueprint_bindings()
                            observed = _read_canonical_publication_receipt_at(
                                prepared_receipt_parent_fd,
                                receipt_path,
                            )
                            if observed != prepared:
                                raise ValueError(
                                    "prepared publication receipt changed during settlement"
                                )
                            _assert_directory_binding(
                                receipt_path.parent,
                                prepared_receipt_parent_fd,
                                label="prepared receipt parent",
                            )
                            current_proof = _read_regular_blueprint_at(
                                draft_parent_fd,
                                draft_path.name,
                                display_path=draft_path,
                                label="blueprint draft",
                                maximum_bytes=prepared_max_blueprint_bytes,
                                maximum_chars=prepared_max_blueprint_chars,
                            )
                            if current_proof != proof:
                                raise ValueError(
                                    "blueprint draft changed during prepared settlement"
                                )
                            observed_target_precondition = (
                                _publication_target_precondition_at(
                                    verified_parent_fd,
                                    verified_path.name,
                                    display_path=verified_path,
                                    maximum_bytes=prepared_max_blueprint_bytes,
                                )
                            )
                            prepared_settlement = (
                                _commit_prepared_publication_settlement(
                                    receipt_path=receipt_path,
                                    identity=prepared_identity,
                                    reason="prepared_request_drift",
                                    observed_target_precondition=(
                                        observed_target_precondition
                                    ),
                                    state_parent_fd=(
                                        direct_journal_parent_fd
                                    ),
                                    receipt_parent_fd=(
                                        prepared_receipt_parent_fd
                                    ),
                                )
                            )
                            settle_publication_admission(prepared_settlement)
                            _assert_directory_binding(
                                receipt_path.parent,
                                prepared_receipt_parent_fd,
                                label="prepared receipt parent",
                            )
                        finally:
                            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
                    return _prepared_publication_nonpublication_result(
                        prepared_settlement
                    )
                else:
                    validated_receipt = _validate_prepared_publication_receipt(
                        prepared_receipt,
                        receipt_bytes=prepared_receipt_bytes,
                        statement=statement,
                        proof=proof,
                        proof_bytes=proof_bytes,
                        manifest=expected_manifest,
                        expected_ids=expected_ids,
                        expected_digest=expected_digest,
                        expected_context_digest=expected_context_digest,
                        verified_path=verified_path,
                        problem_id=str(problem_id),
                        supersedes=supersedes,
                    )
                    with _open_lock_file_at(
                        verified_parent_fd,
                        lock_name,
                        display_path=lock_path,
                    ) as lock_handle:
                        _acquire_publication_lock(
                            lock_handle, display_path=lock_path
                        )
                        try:
                            assert_blueprint_bindings()
                            observed = _read_canonical_publication_receipt_at(
                                prepared_receipt_parent_fd,
                                receipt_path,
                            )
                            if observed != prepared:
                                raise ValueError(
                                    "prepared publication receipt changed during recovery"
                                )
                            _assert_directory_binding(
                                receipt_path.parent,
                                prepared_receipt_parent_fd,
                                label="prepared receipt parent",
                            )
                            current_proof = _read_regular_blueprint_at(
                                draft_parent_fd,
                                draft_path.name,
                                display_path=draft_path,
                                label="blueprint draft",
                                maximum_bytes=prepared_max_blueprint_bytes,
                                maximum_chars=prepared_max_blueprint_chars,
                            )
                            if current_proof != proof:
                                raise ValueError(
                                    "blueprint draft changed during prepared recovery"
                                )
                            current_target_precondition = (
                                _publication_target_precondition_at(
                                    verified_parent_fd,
                                    verified_path.name,
                                    display_path=verified_path,
                                    maximum_bytes=prepared_max_blueprint_bytes,
                                )
                            )
                            persisted_target_precondition = (
                                validated_receipt.get(
                                    "publication_target_precondition"
                                )
                            )
                            existing_metadata = _lstat_at(
                                verified_parent_fd, verified_path.name
                            )
                            if existing_metadata is not None and stat.S_ISREG(
                                existing_metadata.st_mode
                            ):
                                existing = _read_regular_blueprint_at(
                                    verified_parent_fd,
                                    verified_path.name,
                                    display_path=verified_path,
                                    label="verified blueprint",
                                    maximum_bytes=prepared_max_blueprint_bytes,
                                    maximum_chars=prepared_max_blueprint_chars,
                                )
                                exact_publication_exists = existing == proof
                            else:
                                exact_publication_exists = False
                            legacy_v4_replaceable = (
                                persisted_target_precondition is None
                                and current_target_precondition["kind"]
                                in {"absent", "symlink"}
                            )
                            swap_precondition = (
                                current_target_precondition
                                if legacy_v4_replaceable
                                else persisted_target_precondition
                            )
                            swap_recovery_exists = False
                            if swap_precondition is not None:
                                (
                                    _swap_key,
                                    swap_intent_name,
                                    _swap_candidate_record,
                                    _swap_outcome,
                                    _swap_candidate,
                                ) = _conditional_publication_swap_paths(
                                    verified_path.name,
                                    swap_precondition,
                                    proof_bytes,
                                )
                                swap_recovery_exists = _lstat_at(
                                    verified_parent_fd, swap_intent_name
                                ) is not None
                            if (
                                not exact_publication_exists
                                and not swap_recovery_exists
                                and (
                                not legacy_v4_replaceable
                                and (
                                    persisted_target_precondition is None
                                    or current_target_precondition
                                    != persisted_target_precondition
                                )
                                )
                            ):
                                prepared_settlement = (
                                    _commit_prepared_publication_settlement(
                                        receipt_path=receipt_path,
                                        identity=prepared_identity,
                                        reason="prepared_target_collision",
                                        observed_target_precondition=(
                                            current_target_precondition
                                        ),
                                        state_parent_fd=(
                                            direct_journal_parent_fd
                                        ),
                                        receipt_parent_fd=(
                                            prepared_receipt_parent_fd
                                        ),
                                    )
                                )
                                settle_publication_admission(
                                    prepared_settlement
                                )
                                _assert_directory_binding(
                                    receipt_path.parent,
                                    prepared_receipt_parent_fd,
                                    label="prepared receipt parent",
                                )
                                return (
                                    _prepared_publication_nonpublication_result(
                                        prepared_settlement
                                    )
                                )
                            if (
                                not exact_publication_exists
                                or swap_recovery_exists
                            ):
                                if swap_precondition is None:
                                    prepared_settlement = (
                                        _commit_prepared_publication_settlement(
                                            receipt_path=receipt_path,
                                            identity=prepared_identity,
                                            reason="prepared_target_collision",
                                            observed_target_precondition=(
                                                current_target_precondition
                                            ),
                                            state_parent_fd=(
                                                direct_journal_parent_fd
                                            ),
                                            receipt_parent_fd=(
                                                prepared_receipt_parent_fd
                                            ),
                                        )
                                    )
                                    settle_publication_admission(
                                        prepared_settlement
                                    )
                                    _assert_directory_binding(
                                        receipt_path.parent,
                                        prepared_receipt_parent_fd,
                                        label="prepared receipt parent",
                                    )
                                    return (
                                        _prepared_publication_nonpublication_result(
                                            prepared_settlement
                                        )
                                    )
                                published_identity = _conditional_replace_at(
                                    verified_parent_fd,
                                    verified_path.name,
                                    proof_bytes,
                                    expected_precondition=swap_precondition,
                                    display_path=verified_path,
                                    maximum_target_bytes=(
                                        prepared_max_blueprint_bytes
                                    ),
                                )
                                if published_identity is None:
                                    observed_collision = (
                                        _publication_target_precondition_at(
                                            verified_parent_fd,
                                            verified_path.name,
                                            display_path=verified_path,
                                            maximum_bytes=(
                                                prepared_max_blueprint_bytes
                                            ),
                                        )
                                    )
                                    prepared_settlement = (
                                        _commit_prepared_publication_settlement(
                                            receipt_path=receipt_path,
                                            identity=prepared_identity,
                                            reason="prepared_target_collision",
                                            observed_target_precondition=(
                                                observed_collision
                                            ),
                                            state_parent_fd=(
                                                direct_journal_parent_fd
                                            ),
                                            receipt_parent_fd=(
                                                prepared_receipt_parent_fd
                                            ),
                                        )
                                    )
                                    settle_publication_admission(
                                        prepared_settlement
                                    )
                                    _assert_directory_binding(
                                        receipt_path.parent,
                                        prepared_receipt_parent_fd,
                                        label="prepared receipt parent",
                                    )
                                    return (
                                        _prepared_publication_nonpublication_result(
                                            prepared_settlement
                                        )
                                    )
                                try:
                                    assert_blueprint_bindings()
                                except ValueError:
                                    _unlink_if_identity_at(
                                        verified_parent_fd,
                                        verified_path.name,
                                        published_identity,
                                    )
                                    raise
                        finally:
                            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
                    return _prepared_publication_result(
                        receipt=validated_receipt,
                        verified_path=verified_path,
                        receipt_path=receipt_path,
                    )

        # Any path that reaches this point is a fresh verifier admission, not
        # recovery of already-paid evidence.  Enforce the current deployment
        # caps here; prepared recovery above used the receipt-persisted caps.
        if len(proof_bytes) > MAX_BLUEPRINT_BYTES:
            raise ValueError("blueprint draft exceeds VERIFY_MAX_PROOF_BYTES")
        if len(proof) > MAX_BLUEPRINT_CHARS:
            raise ValueError("blueprint draft exceeds VERIFY_MAX_PROOF_CHARS")
        if len(expected_ids) > MAX_PUBLICATION_PROOF_ITEMS:
            raise ValueError("blueprint exceeds the publication proof-item limit")

        if prepared_only:
            raise ValueError(
                "prepared publication recovery is unavailable; refusing verifier dispatch"
            )

        if receipt_path is not None:
            assert direct_journal_parent is not None
            assert publication_admission_identity is not None
            external_admission_slot = (
                observed_publication_admission is not None
                and observed_publication_admission["status"] == "submitted"
                and observed_publication_admission["phase"] == "dispatched"
                and observed_publication_admission.get("effect_dispatch_name")
                is None
            )
            recoverable_external_admission = (
                external_admission_slot
                and all(
                    observed_publication_admission.get(field)
                    == publication_admission_identity.get(field)
                    for field in _PUBLICATION_ADMISSION_IDENTITY_FIELDS
                    if field
                    not in {
                        "proof_digest",
                        "context_digest",
                        "client_source_sha256",
                    }
                )
            )
            exact_external_admission = (
                external_admission_slot
                and all(
                    observed_publication_admission.get(field)
                    == publication_admission_identity.get(field)
                    for field in _PUBLICATION_ADMISSION_IDENTITY_FIELDS
                    if field != "client_source_sha256"
                )
            )
            if (
                recoverable_external_admission
                and not resume_dispatched
                and on_publication_admission_recovery is not None
            ):
                assert observed_publication_admission is not None
                if (
                    archived_prepared is not None
                    or replaceable_settled_receipt is not None
                ):
                    raise VerificationExecutionUnknown(
                        "publication recovery found prepared evidence"
                    )

                recovery_archive_identity: dict[str, Any] | None = None

                def recovery_artifact_observations() -> dict[str, Any]:
                    receipt_observation = _read_canonical_publication_receipt(
                        receipt_path
                    )
                    archive_identities = [prepared_archive_identity]
                    if (
                        recovery_archive_identity is not None
                        and recovery_archive_identity
                        != prepared_archive_identity
                    ):
                        archive_identities.append(recovery_archive_identity)
                    archive_observation = next(
                        (
                            observed
                            for identity in archive_identities
                            if (
                                observed := _read_prepared_publication_archive(
                                    state_parent=direct_journal_parent,
                                    state_parent_fd=direct_journal_parent_fd,
                                    identity=identity,
                                )
                            )
                            is not None
                        ),
                        None,
                    )
                    rollback_observation = _read_receipt_collision_rollback(
                        state_parent=direct_journal_parent,
                        state_parent_fd=direct_journal_parent_fd,
                        receipt_path=receipt_path,
                    )
                    target_observation = _publication_target_precondition_at(
                        verified_parent_fd,
                        verified_path.name,
                        display_path=verified_path,
                        maximum_bytes=ABSOLUTE_MAX_BLUEPRINT_BYTES,
                    )
                    if (
                        receipt_observation is not None
                        or archive_observation is not None
                        or rollback_observation is not None
                        or target_observation["kind"] != "absent"
                    ):
                        raise VerificationExecutionUnknown(
                            "publication recovery found a publication artifact"
                        )
                    return {
                        "canonical_receipt_absent": True,
                        "prepared_archive_absent": True,
                        "receipt_collision_rollback_absent": True,
                        "verified_target": target_observation,
                    }

                observations_before = recovery_artifact_observations()
                raw_admission = _read_direct_finalization_record(
                    _publication_admission_path(
                        direct_journal_parent, receipt_path
                    ),
                    maximum_bytes=_MAX_PUBLICATION_ADMISSION_BYTES,
                    directory_fd=direct_journal_parent_fd,
                )
                if raw_admission is None:
                    raise VerificationExecutionUnknown(
                        "publication admission disappeared before recovery"
                    )
                admission_prior_sha256 = hashlib.sha256(
                    _canonical_json_line_bytes(raw_admission)
                ).hexdigest()
                discovery_request = {
                    "schema_version": (
                        "rethlas_cross_layer_publication_recovery_discovery_v1"
                    ),
                    "admission": dict(raw_admission),
                    "admission_prior_sha256": admission_prior_sha256,
                    "admission_effect_intent_sha256": (
                        observed_publication_admission[
                            "effect_intent_sha256"
                        ]
                    ),
                    "verifier_effect_identity_sha256": (
                        observed_publication_admission[
                            "verifier_effect_identity_sha256"
                        ]
                    ),
                    "artifact_observations": observations_before,
                    "replacement_authority_intent_sha256": (
                        publication_authority_intent_sha256
                    ),
                }
                discovery_response = on_publication_admission_recovery(
                    discovery_request
                )
                if not isinstance(discovery_response, Mapping):
                    raise VerificationExecutionUnknown(
                        "publication recovery discovery returned no authority"
                    )
                authority_payload = dict(discovery_response)
                recovery_blueprint = authority_payload.pop(
                    "recovery_blueprint", None
                )
                if (
                    not isinstance(recovery_blueprint, Mapping)
                    or set(recovery_blueprint)
                    != {"schema_version", "proof_digest", "proof"}
                    or recovery_blueprint.get("schema_version")
                    != _PUBLICATION_RECOVERY_BLUEPRINT_SCHEMA
                    or recovery_blueprint.get("proof_digest")
                    != observed_publication_admission.get("proof_digest")
                    or not isinstance(recovery_blueprint.get("proof"), str)
                ):
                    raise VerificationExecutionUnknown(
                        "publication recovery blueprint is unavailable"
                    )
                recovery_proof = recovery_blueprint["proof"]
                recovery_proof_bytes = recovery_proof.encode("utf-8")
                if (
                    len(recovery_proof_bytes)
                    > ABSOLUTE_MAX_BLUEPRINT_BYTES
                    or len(recovery_proof) > ABSOLUTE_MAX_BLUEPRINT_CHARS
                    or proof_digest(recovery_proof)
                    != recovery_blueprint["proof_digest"]
                ):
                    raise VerificationExecutionUnknown(
                        "publication recovery blueprint digest mismatch"
                    )
                try:
                    recovery_manifest = parse_blueprint(
                        recovery_proof, target_statement=statement
                    )
                except (TypeError, ValueError) as exc:
                    raise VerificationExecutionUnknown(
                        "publication recovery blueprint is invalid"
                    ) from exc
                recovery_checked_ids = list(recovery_manifest.item_ids)
                recovery_context_digest = aggregate_context_digest(
                    recovery_manifest
                )
                if (
                    recovery_context_digest
                    != observed_publication_admission.get("context_digest")
                    or not recovery_checked_ids
                    or len(recovery_checked_ids)
                    > ABSOLUTE_MAX_PUBLICATION_PROOF_ITEMS
                ):
                    raise VerificationExecutionUnknown(
                        "publication recovery blueprint context mismatch"
                    )
                recovery_archive_identity = (
                    _prepared_publication_archive_identity(
                        problem_id=str(problem_id),
                        statement=statement,
                        proof_sha256=recovery_blueprint["proof_digest"],
                        context_digest=recovery_context_digest,
                        proof_context_source_sha256=(
                            observed_publication_admission[
                                "proof_context_source_sha256"
                            ]
                        ),
                        supersedes=list(
                            observed_publication_admission["supersedes"]
                        ),
                        verified_path=verified_path,
                    )
                )
                observations_after_discovery = (
                    recovery_artifact_observations()
                )
                if observations_after_discovery != observations_before:
                    raise VerificationExecutionUnknown(
                        "publication artifacts changed during recovery"
                    )
                recovered_verifier_binding = (
                    _publication_verifier_effect_binding(
                        endpoint=endpoint,
                        timeout_seconds=float(timeout_seconds),
                        api_token=api_token,
                        statement=statement,
                        proof_digest_value=recovery_blueprint[
                            "proof_digest"
                        ],
                        context_digest=recovery_context_digest,
                        checked_item_ids=recovery_checked_ids,
                        verification_profile=verification_profile,
                    )
                )
                if (
                    recovered_verifier_binding["effect_identity_sha256"]
                    != observed_publication_admission.get(
                        "verifier_effect_identity_sha256"
                    )
                ):
                    raise VerificationExecutionUnknown(
                        "publication admission verifier effect identity changed"
                    )
                first_pass = recovered_verifier_binding["pass_bindings"][0]
                first_status = _read_restartable_whole_pass_status(
                    endpoint=endpoint,
                    timeout_seconds=float(timeout_seconds),
                    api_token=api_token,
                    verification_attempt_id=first_pass[2],
                    verification_pass_identity=first_pass[1],
                )
                recovery_request = {
                    "schema_version": (
                        "rethlas_cross_layer_publication_recovery_request_v1"
                    ),
                    "admission": dict(raw_admission),
                    "admission_prior_sha256": admission_prior_sha256,
                    "admission_effect_intent_sha256": (
                        observed_publication_admission[
                            "effect_intent_sha256"
                        ]
                    ),
                    "verifier_effect_identity": dict(
                        recovered_verifier_binding["effect_preimage"]
                    ),
                    "verifier_effect_identity_sha256": (
                        recovered_verifier_binding[
                            "effect_identity_sha256"
                        ]
                    ),
                    "pass_status_observations": [first_status],
                    "artifact_observations": observations_before,
                    "replacement_authority_intent_sha256": (
                        publication_authority_intent_sha256
                    ),
                    "recovery_blueprint": {
                        "schema_version": (
                            _PUBLICATION_RECOVERY_BLUEPRINT_SCHEMA
                        ),
                        "proof_digest": recovery_blueprint[
                            "proof_digest"
                        ],
                        "proof_bytes": len(recovery_proof_bytes),
                        "context_digest": recovery_context_digest,
                        "checked_item_ids_sha256": hashlib.sha256(
                            _canonical_json_line_bytes(
                                recovery_checked_ids
                            )
                        ).hexdigest(),
                    },
                }
                authority = _validate_outer_publication_recovery_authority(
                    authority_payload,
                    admission=observed_publication_admission,
                )
                observations_after_authority = (
                    recovery_artifact_observations()
                )
                if observations_after_authority != observations_before:
                    raise VerificationExecutionUnknown(
                        "publication artifacts changed during recovery"
                    )
                status_after_authority = (
                    _read_restartable_whole_pass_status(
                        endpoint=endpoint,
                        timeout_seconds=float(timeout_seconds),
                        api_token=api_token,
                        verification_attempt_id=first_pass[2],
                        verification_pass_identity=first_pass[1],
                    )
                )
                if status_after_authority != first_status:
                    raise VerificationExecutionUnknown(
                        "whole verifier attempt changed during recovery"
                    )
                certificate_seed = {
                    "schema_version": (
                        _PUBLICATION_RECOVERY_CERTIFICATE_SCHEMA
                    ),
                    "recovery_request": recovery_request,
                    "outer_authority": authority,
                    "artifact_observations_after_authority": (
                        observations_after_authority
                    ),
                    "pass_status_observations_after_authority": [
                        status_after_authority
                    ],
                    "guard_semantics": {
                        "publication_generation_only": True,
                        "verifier_attempt_remains_restartable": True,
                        "missing_later_pass_is_not_negative_evidence": True,
                    },
                }
                _certificate, certificate_sha256 = (
                    _write_publication_recovery_certificate(
                        state_parent=direct_journal_parent,
                        state_parent_fd=direct_journal_parent_fd,
                        seed=certificate_seed,
                    )
                )
                if recovery_artifact_observations() != observations_before:
                    raise VerificationExecutionUnknown(
                        "publication artifacts changed before recovery settlement"
                    )
                _settle_publication_admission(
                    state_parent=direct_journal_parent,
                    state_parent_fd=direct_journal_parent_fd,
                    receipt_path=receipt_path,
                    reason="external_operational_nonpublication",
                    receipt_sha256=None,
                    expected_admission_sha256=admission_prior_sha256,
                    settlement_evidence_sha256=certificate_sha256,
                )
                observed_publication_admission = (
                    _read_publication_admission(
                        state_parent=direct_journal_parent,
                        state_parent_fd=direct_journal_parent_fd,
                        receipt_path=receipt_path,
                    )
                )
            if resume_dispatched:
                if not exact_external_admission:
                    raise VerificationExecutionUnknown(
                        "publication admission is not an exact external dispatch"
                    )
                assert observed_publication_admission is not None
                if (
                    observed_publication_admission.get(
                        "external_authority_intent_sha256"
                    )
                    not in {None, publication_authority_intent_sha256}
                ):
                    raise VerificationExecutionUnknown(
                        "publication admission belongs to another outer authority"
                    )
                current_publication_admission = (
                    observed_publication_admission
                )
            else:
                current_publication_admission = _begin_publication_admission(
                    state_parent=direct_journal_parent,
                    state_parent_fd=direct_journal_parent_fd,
                    receipt_path=receipt_path,
                    identity=publication_admission_identity,
                    permit_exact_successor=(
                        on_verifier_dispatch is not None
                    ),
                    external_authority_intent_sha256=(
                        publication_authority_intent_sha256
                    ),
                )
            if on_verifier_dispatch is not None:
                admission_effect_intent_sha256 = hashlib.sha256(
                    _canonical_json_line_bytes(
                        {
                            "schema_version": (
                                "rethlas_external_publication_effect_intent_v1"
                            ),
                            **{
                                field: current_publication_admission[field]
                                for field in (
                                    *_PUBLICATION_ADMISSION_IDENTITY_FIELDS,
                                    "generation_parent_sha256",
                                )
                            },
                        }
                    )
                ).hexdigest()

        def build_direct_intent_seed(
            target_precondition: Mapping[str, Any],
        ) -> dict[str, Any]:
            return {
                "schema_version": _DIRECT_FINALIZATION_INTENT_SCHEMA,
                "status": "submitted",
                "problem_id": problem_id,
                "statement_source_digest": proof_digest(statement),
                "canonical_target_digest": proof_digest(
                    extract_verification_target(statement)
                ),
                "proof_digest": expected_digest,
                "context_digest": expected_context_digest,
                "checked_item_count": len(expected_ids),
                "checked_item_ids_sha256": hashlib.sha256(
                    _canonical_json_line_bytes(expected_ids)
                ).hexdigest(),
                "draft_path": str(draft_path),
                "verified_path": str(verified_path),
                "receipt_path": str(receipt_path),
                "endpoint": endpoint,
                "verification_profile": verification_profile,
                "verification_quorum": verification_quorum,
                "supersedes": supersedes,
                "proof_context_sha256": (
                    _assert_publication_proof_context_unchanged()
                ),
                "client_source_sha256": (
                    _assert_verification_client_source_unchanged()
                ),
                "publication_generation_parent_sha256": (
                    current_publication_admission[
                        "generation_parent_sha256"
                    ]
                    if current_publication_admission is not None
                    else None
                ),
                "max_intent_bytes": _MAX_DIRECT_FINALIZATION_INTENT_BYTES,
                "publication_target_precondition": dict(target_precondition),
            }

        # A target without a current receipt is explicitly untrusted legacy
        # residue.  Persist its exact state in the receipt so both this turn
        # and a crash recovery replace only the target observed pre-dispatch.
        with _open_lock_file_at(
            verified_parent_fd,
            lock_name,
            display_path=lock_path,
        ) as lock_handle:
            _acquire_publication_lock(lock_handle, display_path=lock_path)
            try:
                assert_blueprint_bindings()
                late_prepared = (
                    _read_canonical_publication_receipt(receipt_path)
                    if receipt_path is not None
                    else None
                )
                late_prepared_is_settled = (
                    replaceable_settled_receipt is not None
                    and late_prepared == replaceable_settled_receipt
                )
                if late_prepared is None or late_prepared_is_settled:
                    # A successful publication receipt is the terminal record
                    # for a direct dispatch.  Recheck it under both locks
                    # before treating a dispatch-only journal as indeterminate.
                    # If there is no receipt, replay a durable negative result
                    # before consulting the mutable publication target.
                    if (
                        receipt_path is not None
                        and on_verifier_dispatch is None
                        and not resume_dispatched
                    ):
                        absent_target_precondition = {
                            "kind": "absent",
                            "st_dev": None,
                            "st_ino": None,
                            "st_size": None,
                            "st_mtime_ns": None,
                            "content_sha256": None,
                        }
                        direct_intent_probe = build_direct_intent_seed(
                            absent_target_precondition
                        )
                        prior_intent_path, _prior_dispatch, _prior_result = (
                            _direct_finalization_paths(
                                receipt_path,
                                _direct_finalization_journal_key(
                                    direct_intent_probe
                                ),
                                journal_parent=direct_journal_parent,
                            )
                        )
                        prior_intent = _read_direct_finalization_record(
                            prior_intent_path,
                            maximum_bytes=(
                                _ABSOLUTE_MAX_DIRECT_FINALIZATION_INTENT_BYTES
                            ),
                            directory_fd=direct_journal_parent_fd,
                        )
                        if prior_intent is None:
                            legacy_prior_paths = (
                                _legacy_direct_finalization_paths(
                                    receipt_path, expected_digest
                                )
                            )
                            if _legacy_direct_finalization_paths_are_addressable(
                                legacy_prior_paths
                            ):
                                prior_intent_path = legacy_prior_paths[0]
                                prior_intent = _read_direct_finalization_record(
                                    prior_intent_path,
                                    maximum_bytes=(
                                        _ABSOLUTE_MAX_DIRECT_FINALIZATION_INTENT_BYTES
                                    ),
                                )
                        if prior_intent is not None:
                            prior_target_precondition = (
                                _validate_publication_target_precondition(
                                    prior_intent.get(
                                        "publication_target_precondition"
                                    )
                                )
                            )
                            (
                                _prior_intent,
                                _prior_intent_path,
                                _prior_dispatch_path,
                                _prior_result_path,
                                replayed_direct_result,
                            ) = _prepare_direct_finalization(
                                receipt_path=receipt_path,
                                intent_seed=build_direct_intent_seed(
                                    prior_target_precondition
                                ),
                                journal_parent=direct_journal_parent,
                                journal_parent_fd=direct_journal_parent_fd,
                            )
                            if replayed_direct_result is not None:
                                settle_direct_nonpublication_admission(
                                    replayed_direct_result
                                )
                                return replayed_direct_result
                    publication_target_precondition = (
                        _publication_target_precondition_at(
                            verified_parent_fd,
                            verified_path.name,
                            display_path=verified_path,
                        )
                    )
            finally:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
        if late_prepared is not None and not late_prepared_is_settled:
            # Another publisher committed its durable verifier evidence in
            # the initial read-to-lock window.  Restart only the local
            # preflight so the prepared receipt is authenticated and promoted
            # without issuing a profile request or verifier POST.
            assert verification_effect_lock_handle is not None
            fcntl.flock(
                verification_effect_lock_handle.fileno(), fcntl.LOCK_UN
            )
            verification_effect_lock_handle.close()
            verification_effect_lock_handle = None
            if publication_identity_lock_handle is not None:
                fcntl.flock(
                    publication_identity_lock_handle.fileno(),
                    fcntl.LOCK_UN,
                )
                publication_identity_lock_handle.close()
                publication_identity_lock_handle = None
            return verify_blueprint_file(
                statement=statement,
                draft_path=draft_path,
                verified_path=verified_path,
                endpoint=endpoint,
                verification_deadline_utc=verification_deadline_utc,
                timeout_seconds=timeout_seconds,
                api_token=api_token,
                receipt_path=receipt_path,
                problem_id=problem_id,
                blueprint_root=blueprint_root,
                publication_state_root=publication_state_root,
                verification_quorum=verification_quorum,
                supersedes=supersedes,
                verification_profile=verification_profile,
                on_verifier_dispatch=on_verifier_dispatch,
                prepared_only=prepared_only,
                publication_authority_intent_sha256=(
                    publication_authority_intent_sha256
                ),
                on_publication_admission_recovery=(
                    on_publication_admission_recovery
                ),
                resume_dispatched=resume_dispatched,
            )
        if (
            receipt_path is not None
            and on_verifier_dispatch is None
            and not resume_dispatched
        ):
            direct_intent_seed = build_direct_intent_seed(
                publication_target_precondition
            )
            (
                direct_intent,
                _direct_intent_path,
                direct_dispatch_path,
                direct_result_path,
                replayed_direct_result,
            ) = _prepare_direct_finalization(
                receipt_path=receipt_path,
                intent_seed=direct_intent_seed,
                journal_parent=direct_journal_parent,
                journal_parent_fd=direct_journal_parent_fd,
            )
            if replayed_direct_result is not None:
                settle_direct_nonpublication_admission(
                    replayed_direct_result
                )
                return replayed_direct_result
            direct_finalization_state = (
                direct_intent,
                direct_dispatch_path,
                direct_result_path,
            )
            admission_effect_intent_sha256 = hashlib.sha256(
                _canonical_json_line_bytes(direct_intent)
            ).hexdigest()
            assert direct_journal_parent is not None
            current_publication_admission = (
                _bind_publication_admission_effect_intent(
                    state_parent=direct_journal_parent,
                    state_parent_fd=direct_journal_parent_fd,
                    receipt_path=receipt_path,
                    effect_intent_sha256=(
                        admission_effect_intent_sha256
                    ),
                    effect_dispatch_name=direct_dispatch_path.name,
                )
            )

        def durable_nonpublication(result_value: Mapping[str, Any]) -> Dict[str, Any]:
            committed_result = dict(result_value)
            if direct_finalization_state is not None:
                direct_intent, direct_dispatch_path, direct_result_path = (
                    direct_finalization_state
                )
                assert direct_journal_parent is not None
                _assert_directory_binding(
                    direct_journal_parent,
                    direct_journal_parent_fd,
                    label="direct finalization journal parent",
                )
                committed_result = _commit_direct_finalization_result(
                    intent=direct_intent,
                    dispatch_path=direct_dispatch_path,
                    result_path=direct_result_path,
                    result=result_value,
                    journal_parent_fd=direct_journal_parent_fd,
                )
            if receipt_path is not None:
                assert direct_journal_parent is not None
                admission = _read_publication_admission(
                    state_parent=direct_journal_parent,
                    state_parent_fd=direct_journal_parent_fd,
                    receipt_path=receipt_path,
                )
                if admission is not None and admission["status"] == "submitted":
                    _settle_publication_admission(
                        state_parent=direct_journal_parent,
                        state_parent_fd=direct_journal_parent_fd,
                        receipt_path=receipt_path,
                        reason=direct_nonpublication_settlement_reason(
                            result_value
                        ),
                        receipt_sha256=None,
                    )
                _assert_directory_binding(
                    direct_journal_parent,
                    direct_journal_parent_fd,
                    label="direct finalization journal parent",
                )
            return committed_result
        (
            caller_instance_id,
            caller_pid,
            caller_start_sha256,
        ) = _verification_caller_binding()
        physical_turn_request_sha256 = hashlib.sha256(
            json.dumps(
                {
                    "schema_version": "rethlas_verifier_physical_turn_request_v1",
                    "caller_instance_id": caller_instance_id,
                    "endpoint": endpoint,
                    "verification_profile": verification_profile,
                    "statement_target_digest": proof_digest(
                        extract_verification_target(statement)
                    ),
                    "proof_digest": expected_digest,
                    "context_digest": expected_context_digest,
                    "proof_context_sha256": (
                        _assert_publication_proof_context_unchanged()
                    ),
                    "client_source_sha256": (
                        _assert_verification_client_source_unchanged()
                    ),
                    "publication_generation_parent_sha256": (
                        current_publication_admission[
                            "generation_parent_sha256"
                        ]
                        if current_publication_admission is not None
                        else None
                    ),
                    "checked_item_ids": expected_ids,
                    "draft_path": str(draft_path),
                },
                allow_nan=False,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        if physical_turn_request_sha256 in _FAILED_PHYSICAL_TURN_REQUESTS:
            raise VerificationSameTurnRetryForbidden(
                "verifier retry is forbidden in the same physical turn"
            )
        # Expiry is a gate on new verifier effects, not on replaying durable
        # local evidence.  All archive, canonical-receipt, and direct-result
        # recovery paths above therefore remain available after this deadline.
        remaining_seconds = (
            deadline - datetime.now(timezone.utc)
        ).total_seconds()
        if remaining_seconds <= 0:
            raise ValueError("verification_deadline_utc has already expired")
        transport_timeout = min(
            float(timeout_seconds), max(1.0, remaining_seconds + 5.0)
        )
        if publication_verifier_binding is None:
            publication_verifier_binding = (
                _publication_verifier_effect_binding(
                    endpoint=endpoint,
                    timeout_seconds=transport_timeout,
                    api_token=api_token,
                    statement=statement,
                    proof_digest_value=expected_digest,
                    context_digest=expected_context_digest,
                    checked_item_ids=expected_ids,
                    verification_profile=verification_profile,
                )
            )
        raw_profile = publication_verifier_binding["raw_profile"]
        expected_passes = publication_verifier_binding["expected_passes"]
        verifier_service_version = publication_verifier_binding[
            "verifier_service_version"
        ]
        pass_bindings = publication_verifier_binding["pass_bindings"]
        verifier_effect_identity_sha256 = publication_verifier_binding[
            "effect_identity_sha256"
        ]
        if receipt_path is not None:
            assert direct_journal_parent is not None
            current_publication_admission = (
                _bind_publication_admission_verifier_effect_identity(
                    state_parent=direct_journal_parent,
                    state_parent_fd=direct_journal_parent_fd,
                    receipt_path=receipt_path,
                    verifier_effect_identity_sha256=(
                        verifier_effect_identity_sha256
                    ),
                )
            )
            if resume_dispatched:
                verifier_dispatch_committed = True
        verification_payloads: list[dict[str, Any]] = []
        verification_passes: list[dict[str, Any]] = []
        for pass_index in range(verification_quorum):
            remaining_seconds = (
                deadline - datetime.now(timezone.utc)
            ).total_seconds()
            if remaining_seconds <= 0:
                raise ValueError("verification deadline expired before quorum settled")
            expected_pass, pass_identity, attempt_id = pass_bindings[
                pass_index
            ]
            request_payload: Dict[str, Any] = {
                "statement": statement,
                "proof": proof,
                "verification_deadline_utc": verification_deadline_utc,
                "verification_attempt_id": attempt_id,
                "verification_pass_index": pass_index + 1,
                "verification_pass_identity": pass_identity,
                "verification_caller_instance_id": caller_instance_id,
            }
            if _endpoint_uses_local_lifeline(endpoint):
                request_payload.update(
                    {
                        "verification_caller_pid": caller_pid,
                        "verification_caller_start_sha256": caller_start_sha256,
                    }
                )
            request_kwargs: Dict[str, Any] = {
                "json": request_payload,
                "timeout": min(float(timeout_seconds), remaining_seconds + 5.0),
            }
            if api_token:
                request_kwargs["headers"] = {"Authorization": f"Bearer {api_token}"}
            if not verifier_dispatch_committed:
                effect_sha256 = admission_effect_intent_sha256
                effect_dispatch_name: str | None = None
                if effect_sha256 is None:
                    effect_sha256 = physical_turn_request_sha256
                if direct_finalization_state is not None:
                    effect_dispatch_name = direct_finalization_state[1].name
                if receipt_path is not None:
                    assert direct_journal_parent is not None
                    current_publication_admission = (
                        _mark_publication_admission_dispatched(
                            state_parent=direct_journal_parent,
                            state_parent_fd=direct_journal_parent_fd,
                            receipt_path=receipt_path,
                            effect_intent_sha256=effect_sha256,
                            effect_dispatch_name=effect_dispatch_name,
                        )
                    )
                try:
                    if on_verifier_dispatch is not None:
                        on_verifier_dispatch()
                    elif direct_finalization_state is not None:
                        (
                            direct_intent,
                            direct_dispatch_path,
                            _direct_result_path,
                        ) = direct_finalization_state
                        assert direct_journal_parent is not None
                        _assert_directory_binding(
                            direct_journal_parent,
                            direct_journal_parent_fd,
                            label="direct finalization journal parent",
                        )
                        assert_blueprint_bindings()
                        _commit_direct_finalization_dispatch(
                            intent=direct_intent,
                            dispatch_path=direct_dispatch_path,
                            journal_parent_fd=direct_journal_parent_fd,
                        )
                        _assert_directory_binding(
                            direct_journal_parent,
                            direct_journal_parent_fd,
                            label="direct finalization journal parent",
                        )
                        assert_blueprint_bindings()
                except Exception:
                    if receipt_path is not None:
                        assert direct_journal_parent is not None
                        current_publication_admission = (
                            _reset_publication_admission_after_failed_dispatch(
                                state_parent=direct_journal_parent,
                                state_parent_fd=direct_journal_parent_fd,
                                receipt_path=receipt_path,
                                effect_intent_sha256=effect_sha256,
                            )
                        )
                    raise
                verifier_dispatch_committed = True
            if direct_finalization_state is not None:
                assert direct_journal_parent is not None
                _assert_directory_binding(
                    direct_journal_parent,
                    direct_journal_parent_fd,
                    label="direct finalization journal parent",
                )
                assert_blueprint_bindings()
            response = requests.post(endpoint, **request_kwargs)
            try:
                _raise_for_verification_service_error(response)
                raw_payload = response.json()
                payload = validate_service_response(
                    raw_payload,
                    expected_proof_digest=expected_digest,
                    expected_checked_item_ids=expected_ids,
                    expected_context_digest=expected_context_digest,
                    expected_manifest=expected_manifest,
                    expected_verification_attempt_id=attempt_id,
                    expected_verification_pass_index=pass_index + 1,
                )
                if (
                    payload["verifier_model"] != expected_pass["model"]
                    or payload["verifier_reasoning_effort"]
                    != expected_pass["reasoning_effort"]
                    or payload["verifier_service_version"]
                    != verifier_service_version
                ):
                    raise ValueError(
                        "verification result differs from selected profile"
                    )
                response_sha256 = hashlib.sha256(
                    json.dumps(
                        payload,
                        allow_nan=False,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest()
            except VerificationExecutionUnknown:
                raise
            except VerificationOperationalFailure as exc:
                _FAILED_PHYSICAL_TURN_REQUESTS.add(
                    physical_turn_request_sha256
                )
                return durable_nonpublication(
                    _operational_verifier_failure_result(
                        pass_index=pass_index + 1,
                        verification_passes=verification_passes,
                        failure=exc,
                    )
                )
            except (
                requests.HTTPError,
                TypeError,
                ValueError,
                UnicodeError,
                RecursionError,
            ):
                # The POST itself returned a concrete HTTP response.  Whatever
                # its status or shape, no local publication has happened, so
                # this is a definite bounded negative terminal rather than an
                # execution-unknown crash window.  Preserve the same-turn
                # replay fence even for direct (non-Core) callers.
                _FAILED_PHYSICAL_TURN_REQUESTS.add(
                    physical_turn_request_sha256
                )
                return durable_nonpublication(
                    _invalid_verifier_response_result(
                        pass_index=pass_index + 1,
                        verification_passes=verification_passes,
                    )
                )
            verification_payloads.append(dict(payload))
            verification_passes.append(
                {
                    "pass_index": pass_index + 1,
                    "verification_attempt_id": attempt_id,
                    "verifier_run_id": payload["verifier_run_id"],
                    "verifier_model": payload["verifier_model"],
                    "verifier_reasoning_effort": payload[
                        "verifier_reasoning_effort"
                    ],
                    "verifier_service_version": payload[
                        "verifier_service_version"
                    ],
                    "verification_role": payload["verification_role"],
                    "response_sha256": response_sha256,
                    "verdict": payload["verdict"],
                }
            )
            if payload["verdict"] != "correct":
                result = dict(payload)
                result["published"] = False
                result["verification_quorum"] = verification_quorum
                result["verification_passes"] = verification_passes
                return durable_nonpublication(result)

        if (
            len(
                {
                    verification_pass["verification_attempt_id"]
                    for verification_pass in verification_passes
                }
            )
            != verification_quorum
            or len(
                {
                    verification_pass["verifier_run_id"]
                    for verification_pass in verification_passes
                }
            )
            != verification_quorum
        ):
            return durable_nonpublication(
                _nonindependent_verifier_quorum_result(
                    verification_passes=verification_passes
                )
            )

        payload = verification_payloads[-1]
        result = dict(payload)
        result["published"] = False
        result["verification_quorum"] = verification_quorum
        result["verification_passes"] = verification_passes

        receipt: dict[str, Any] | None = None
        if receipt_path is not None:
            receipt = {
                "schema_version": "rethlas-publication-v6",
                "state": "active",
                "problem_id": problem_id,
                "statement_source_digest": proof_digest(statement),
                "canonical_target_digest": proof_digest(
                    extract_verification_target(statement)
                ),
                "proof_digest": expected_digest,
                "context_digest": expected_context_digest,
                "adaptive_context_digest": payload["adaptive_context_digest"],
                "item_context_attestations": payload[
                    "item_context_attestations"
                ],
                "checked_item_ids": expected_ids,
                "verified_path": str(verified_path),
                "published_bytes": len(proof_bytes),
                "published_at_utc": datetime.now(timezone.utc).isoformat(),
                "verification_quorum": verification_quorum,
                "verification_passes": verification_passes,
                "supersedes": supersedes,
                "publication_target_precondition": (
                    publication_target_precondition
                ),
                "proof_context": {
                    "schema_version": PUBLICATION_PROOF_CONTEXT_SCHEMA,
                    "source_sha256": _assert_publication_proof_context_unchanged(),
                    "proof_item_schema_version": PROOF_ITEM_SCHEMA_VERSION,
                    "proof_context_schema_version": PROOF_CONTEXT_SCHEMA_VERSION,
                    "aggregate_context_schema_version": (
                        AGGREGATE_CONTEXT_SCHEMA_VERSION
                    ),
                    "adaptive_aggregate_context_schema_version": (
                        ADAPTIVE_AGGREGATE_CONTEXT_SCHEMA_VERSION
                    ),
                },
                "verification_limits": {
                    "context_max_chars": VERIFY_CONTEXT_MAX_CHARS,
                    "max_expansion_rounds": MAX_EXPANSION_ROUNDS,
                    "max_expanded_proofs": MAX_EXPANDED_PROOFS,
                    "max_expanded_proof_chars": MAX_EXPANDED_PROOF_CHARS,
                    "max_proof_items": MAX_PUBLICATION_PROOF_ITEMS,
                    "max_receipt_bytes": MAX_PUBLICATION_RECEIPT_BYTES,
                    "max_blueprint_bytes": MAX_BLUEPRINT_BYTES,
                    "max_blueprint_chars": MAX_BLUEPRINT_CHARS,
                },
            }
            receipt_encoded = (
                json.dumps(
                    receipt,
                    allow_nan=False,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            ).encode("utf-8")
            if len(receipt_encoded) > MAX_PUBLICATION_RECEIPT_BYTES:
                return durable_nonpublication({
                    "published": False,
                    "verdict": "wrong",
                    "verification_status": "final",
                    "publication_blocked_reason": "receipt_over_limit",
                    "publication_receipt_bytes": len(receipt_encoded),
                    "publication_receipt_max_bytes": (
                        MAX_PUBLICATION_RECEIPT_BYTES
                    ),
                    "repair_hints": (
                        "Reduce the number of proof items or expansion attestations "
                        "before requesting a new publication attempt."
                    ),
                })

        assert_blueprint_bindings()
        with _open_lock_file_at(
            verified_parent_fd,
            lock_name,
            display_path=lock_path,
        ) as lock_handle:
            _acquire_publication_lock(lock_handle, display_path=lock_path)
            try:
                assert_blueprint_bindings()
                current_proof = _read_regular_blueprint_at(
                    draft_parent_fd,
                    draft_path.name,
                    display_path=draft_path,
                    label="blueprint draft",
                )
                if proof_digest(current_proof) != expected_digest:
                    raise ValueError("blueprint draft changed during verification")

                current_target_precondition = _publication_target_precondition_at(
                    verified_parent_fd,
                    verified_path.name,
                    display_path=verified_path,
                )
                if current_target_precondition != publication_target_precondition:
                    return durable_nonpublication(
                        _publication_target_collision_result(
                            verification_passes=verification_passes,
                        )
                    )
                if receipt_path is not None:
                    if prepared_receipt_parent_fd < 0:
                        prepared_receipt_parent_fd = (
                            _open_or_create_directory_durable(
                                receipt_path.parent,
                                label="receipt parent",
                            )
                        )
                    try:
                        competing_receipt = (
                            _read_canonical_publication_receipt_at(
                                prepared_receipt_parent_fd,
                                receipt_path,
                            )
                        )
                        _assert_directory_binding(
                            receipt_path.parent,
                            prepared_receipt_parent_fd,
                            label="receipt parent",
                        )
                    except ValueError:
                        return durable_nonpublication(
                            _publication_target_collision_result(
                                verification_passes=verification_passes,
                            )
                        )
                    if (
                        competing_receipt is not None
                        and competing_receipt != replaceable_settled_receipt
                    ):
                        return durable_nonpublication(
                            _publication_target_collision_result(
                                verification_passes=verification_passes,
                            )
                        )

                assert_blueprint_bindings()
                if receipt is not None and (
                    _assert_publication_proof_context_unchanged()
                    != receipt["proof_context"]["source_sha256"]
                ):
                    raise RuntimeError(
                        "publication proof-context binding changed before publication"
                    )

                receipt_identity: tuple[int, int] | None = None
                if receipt_path is not None:
                    assert receipt is not None
                    assert direct_journal_parent is not None
                    assert prepared_archive_identity is not None
                    archived_prepared = _commit_prepared_publication_archive(
                        state_parent=direct_journal_parent,
                        state_parent_fd=direct_journal_parent_fd,
                        identity=prepared_archive_identity,
                        receipt=receipt,
                        receipt_path=receipt_path,
                    )
                    try:
                        receipt_identity = _write_receipt_atomic_at(
                            prepared_receipt_parent_fd,
                            receipt_path,
                            receipt,
                        )
                        assert_blueprint_bindings()
                    except Exception:
                        if receipt_identity is not None:
                            _unlink_if_identity_at(
                                prepared_receipt_parent_fd,
                                receipt_path.name,
                                receipt_identity,
                            )
                        raise

                # The durable receipt is also the recovery record for the
                # cross-directory publication.  Once it exists, a crash before
                # this replace can deterministically promote these exact bytes
                # without repeating either verifier pass.
                published_identity = _conditional_replace_at(
                    verified_parent_fd,
                    verified_path.name,
                    proof_bytes,
                    expected_precondition=publication_target_precondition,
                    display_path=verified_path,
                    retain_displaced=True,
                )
                if published_identity is None:
                    if receipt_path is not None:
                        observed_prepared = (
                            _read_canonical_publication_receipt_at(
                                prepared_receipt_parent_fd,
                                receipt_path,
                            )
                        )
                        expected_receipt_bytes = _canonical_json_line_bytes(
                            receipt
                        )
                        if (
                            observed_prepared is None
                            or observed_prepared
                            != (receipt, expected_receipt_bytes)
                        ):
                            raise ValueError(
                                "prepared receipt changed during target collision"
                            )
                        prepared_identity = (
                            _prepared_publication_receipt_identity(
                                receipt,
                                receipt_bytes=expected_receipt_bytes,
                                problem_id=str(problem_id),
                                verified_path=verified_path,
                            )
                        )
                        prepared_settlement = (
                            _commit_prepared_publication_settlement(
                                receipt_path=receipt_path,
                                identity=prepared_identity,
                                reason="prepared_target_collision",
                                observed_target_precondition=(
                                    _publication_target_precondition_at(
                                        verified_parent_fd,
                                        verified_path.name,
                                        display_path=verified_path,
                                    )
                                ),
                                state_parent_fd=direct_journal_parent_fd,
                                receipt_parent_fd=(
                                    prepared_receipt_parent_fd
                                ),
                            )
                        )
                        settle_publication_admission(prepared_settlement)
                        return durable_nonpublication(
                            _prepared_publication_nonpublication_result(
                                prepared_settlement
                            )
                        )
                    return durable_nonpublication(
                        _publication_target_collision_result(
                            verification_passes=verification_passes,
                        )
                    )
                try:
                    assert_blueprint_bindings()
                except ValueError:
                    if receipt_path is not None and receipt_identity is not None:
                        _unlink_if_identity_at(
                            prepared_receipt_parent_fd,
                            receipt_path.name,
                            receipt_identity,
                        )
                    _rollback_retained_conditional_replace_at(
                        verified_parent_fd,
                        verified_path.name,
                        proof_bytes,
                        expected_precondition=publication_target_precondition,
                        published_identity=published_identity,
                        display_path=verified_path,
                    )
                    raise
                if receipt_path is not None:
                    assert receipt is not None
                    expected_receipt = (
                        receipt,
                        _canonical_json_line_bytes(receipt),
                    )
                    try:
                        _assert_directory_binding(
                            receipt_path.parent,
                            prepared_receipt_parent_fd,
                            label="receipt parent",
                        )
                        if _read_canonical_publication_receipt_at(
                            prepared_receipt_parent_fd,
                            receipt_path,
                        ) != expected_receipt:
                            raise ValueError(
                                "publication receipt changed after target replace"
                            )
                    except ValueError:
                        replacement_receipt_parent_fd = (
                            _open_or_create_directory_durable(
                                receipt_path.parent,
                                label="replacement receipt parent",
                            )
                        )
                        try:
                            competing = _read_canonical_publication_receipt_at(
                                replacement_receipt_parent_fd,
                                receipt_path,
                            )
                            if competing is None:
                                _write_receipt_atomic_at(
                                    replacement_receipt_parent_fd,
                                    receipt_path,
                                    receipt,
                                )
                            elif competing != expected_receipt:
                                (
                                    _rollback_operation_key,
                                    _rollback_intent_name,
                                    _rollback_candidate_record_name,
                                    _rollback_outcome_name,
                                    rollback_candidate_name,
                                ) = _conditional_publication_swap_paths(
                                    verified_path.name,
                                    publication_target_precondition,
                                    proof_bytes,
                                )
                                rollback_intent = (
                                    _commit_receipt_collision_rollback(
                                        state_parent=direct_journal_parent,
                                        state_parent_fd=(
                                            direct_journal_parent_fd
                                        ),
                                        receipt_path=receipt_path,
                                        archive_identity=(
                                            prepared_archive_identity
                                        ),
                                        receipt_sha256=hashlib.sha256(
                                            expected_receipt[1]
                                        ).hexdigest(),
                                        competing_receipt_sha256=(
                                            hashlib.sha256(
                                                competing[1]
                                            ).hexdigest()
                                        ),
                                        expected_target_precondition=(
                                            publication_target_precondition
                                        ),
                                        published_identity=published_identity,
                                        candidate_name=(
                                            rollback_candidate_name
                                        ),
                                    )
                                )
                                observed_after_rollback = (
                                    _recover_receipt_collision_rollback_at(
                                        verified_parent_fd,
                                        verified_path.name,
                                        rollback=rollback_intent,
                                        display_path=verified_path,
                                    )
                                )
                                own_identity = (
                                    _prepared_publication_receipt_identity(
                                        receipt,
                                        receipt_bytes=expected_receipt[1],
                                        problem_id=str(problem_id),
                                        verified_path=verified_path,
                                    )
                                )
                                receipt_collision_settlement = (
                                    _commit_prepared_publication_settlement(
                                        receipt_path=receipt_path,
                                        identity=own_identity,
                                        reason="prepared_target_collision",
                                        observed_target_precondition=(
                                            observed_after_rollback
                                        ),
                                        state_parent_fd=(
                                            direct_journal_parent_fd
                                        ),
                                        receipt_parent_fd=(
                                            prepared_receipt_parent_fd
                                        ),
                                    )
                                )
                                _settle_receipt_collision_rollback(
                                    state_parent=direct_journal_parent,
                                    state_parent_fd=(
                                        direct_journal_parent_fd
                                    ),
                                    receipt_path=receipt_path,
                                    settlement=(
                                        receipt_collision_settlement
                                    ),
                                )
                                settle_publication_admission(
                                    receipt_collision_settlement
                                )
                                os.close(replacement_receipt_parent_fd)
                                return durable_nonpublication(
                                    _prepared_publication_nonpublication_result(
                                        receipt_collision_settlement
                                    )
                                )
                            _assert_directory_binding(
                                receipt_path.parent,
                                replacement_receipt_parent_fd,
                                label="replacement receipt parent",
                            )
                        except Exception:
                            os.close(replacement_receipt_parent_fd)
                            raise
                        if receipt_identity is not None:
                            _unlink_if_identity_at(
                                prepared_receipt_parent_fd,
                                receipt_path.name,
                                receipt_identity,
                            )
                        os.close(prepared_receipt_parent_fd)
                        prepared_receipt_parent_fd = (
                            replacement_receipt_parent_fd
                        )
                _finalize_retained_conditional_replace_at(
                    verified_parent_fd,
                    verified_path.name,
                    proof_bytes,
                    expected_precondition=publication_target_precondition,
                    published_identity=published_identity,
                    display_path=verified_path,
                )

            finally:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)

        result["published"] = True
        result["published_path"] = str(verified_path)
        if receipt_path is not None:
            result["publication_receipt_path"] = str(receipt_path)
        return result
    except BaseException:
        if (
            physical_turn_request_sha256 is not None
            and verifier_dispatch_committed
        ):
            _FAILED_PHYSICAL_TURN_REQUESTS.add(physical_turn_request_sha256)
        raise
    finally:
        if verification_effect_lock_handle is not None:
            fcntl.flock(verification_effect_lock_handle.fileno(), fcntl.LOCK_UN)
            verification_effect_lock_handle.close()
        if publication_identity_lock_handle is not None:
            fcntl.flock(publication_identity_lock_handle.fileno(), fcntl.LOCK_UN)
            publication_identity_lock_handle.close()
        if direct_journal_parent_fd >= 0:
            os.close(direct_journal_parent_fd)
        if prepared_receipt_parent_fd >= 0:
            os.close(prepared_receipt_parent_fd)
        if verified_parent_fd >= 0:
            os.close(verified_parent_fd)
        if draft_parent_fd >= 0:
            os.close(draft_parent_fd)
        if blueprint_root_fd >= 0:
            os.close(blueprint_root_fd)


__all__ = [
    "expected_attestation",
    "proof_digest",
    "validate_service_response",
    "verify_blueprint_file",
]
