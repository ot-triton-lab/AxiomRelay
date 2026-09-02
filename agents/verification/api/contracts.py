"""Strict construction and validation for verification API output."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping, Sequence


_OUTPUT_FIELDS = (
    "output_schema_version",
    "verification_report",
    "verification_status",
    "verdict",
    "repair_hints",
    "needs_expanded_proofs",
    "checked_item_ids",
    "proof_digest",
    "context_digest",
)
_REPORT_FIELDS = ("summary", "critical_errors", "gaps")
_FINDING_FIELDS = ("location", "issue")
_VERDICTS = ("correct", "wrong")
_VERIFICATION_STATUSES = ("final", "needs_context")
_EXPANSION_REQUEST_FIELDS = ("id", "reason")
OUTPUT_SCHEMA_VERSION = 2


def _format_keys(keys: Sequence[object]) -> str:
    ordered = sorted(keys, key=lambda key: (type(key).__name__, repr(key)))
    return ", ".join(repr(key) for key in ordered)


def _validate_exact_keys(
    value: Mapping[object, object], expected: Sequence[str], path: str
) -> None:
    missing = [key for key in expected if key not in value]
    if missing:
        raise ValueError(f"{path} is missing required properties: {_format_keys(missing)}")

    expected_set = set(expected)
    extras = [key for key in value if key not in expected_set]
    if extras:
        raise ValueError(f"{path} contains unexpected properties: {_format_keys(extras)}")


def _validate_findings(value: object, path: str) -> list[Mapping[str, Any]]:
    if not isinstance(value, list):
        raise ValueError(f"{path} must be a list")

    for index, finding in enumerate(value):
        finding_path = f"{path}[{index}]"
        if not isinstance(finding, dict):
            raise ValueError(f"{finding_path} must be an object")
        _validate_exact_keys(finding, _FINDING_FIELDS, finding_path)

        for field in _FINDING_FIELDS:
            field_path = f"{finding_path}.{field}"
            field_value = finding[field]
            if not isinstance(field_value, str):
                raise ValueError(f"{field_path} must be a string")
            if not field_value:
                raise ValueError(f"{field_path} must be non-empty")

    return value


def _validate_checked_item_ids(value: object, path: str) -> list[str]:
    if not isinstance(value, list):
        raise ValueError(f"{path} must be a list")

    seen: set[str] = set()
    for index, item_id in enumerate(value):
        item_path = f"{path}[{index}]"
        if not isinstance(item_id, str):
            raise ValueError(f"{item_path} must be a string")
        if not item_id:
            raise ValueError(f"{item_path} must be non-empty")
        if item_id in seen:
            raise ValueError(f"{path} contains duplicate item {item_id!r}")
        seen.add(item_id)

    return value


def _validate_expansion_requests(value: object) -> list[Mapping[str, str]]:
    if not isinstance(value, list):
        raise ValueError("needs_expanded_proofs must be a list")
    seen: set[str] = set()
    for index, request in enumerate(value):
        path = f"needs_expanded_proofs[{index}]"
        if not isinstance(request, dict):
            raise ValueError(f"{path} must be an object")
        _validate_exact_keys(request, _EXPANSION_REQUEST_FIELDS, path)
        request_id = request["id"]
        reason = request["reason"]
        if not isinstance(request_id, str) or not request_id:
            raise ValueError(f"{path}.id must be a non-empty string")
        if request_id in seen:
            raise ValueError(
                f"needs_expanded_proofs contains duplicate id {request_id!r}"
            )
        seen.add(request_id)
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError(f"{path}.reason must contain non-whitespace text")
    return value


def _normalize_expected_checked_item_ids(value: object) -> list[str]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValueError("expected_checked_item_ids must be a sequence of strings")

    normalized = list(value)
    seen: set[str] = set()
    for index, item_id in enumerate(normalized):
        if not isinstance(item_id, str):
            raise ValueError(
                f"expected_checked_item_ids[{index}] must be a string"
            )
        if not item_id:
            raise ValueError(
                f"expected_checked_item_ids[{index}] must be non-empty"
            )
        if item_id in seen:
            raise ValueError(
                f"expected_checked_item_ids contains duplicate item {item_id!r}"
            )
        seen.add(item_id)
    return normalized


def _validate_expected_digest(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    if not value:
        raise ValueError(f"{name} must be non-empty")
    return value


def validate_verification_output(
    payload: Mapping[str, Any],
    expected_checked_item_ids: Sequence[str],
    expected_proof_digest: str,
    expected_context_digest: str,
) -> dict[str, Any]:
    """Validate output against its shape, invariants, and server provenance.

    The returned object is a deep copy. Neither ``payload`` nor any expected
    value is modified.
    """

    if not isinstance(payload, dict):
        raise ValueError("verification output must be an object")
    _validate_exact_keys(payload, _OUTPUT_FIELDS, "verification output")

    if payload["output_schema_version"] != OUTPUT_SCHEMA_VERSION:
        raise ValueError(
            f"output_schema_version must be {OUTPUT_SCHEMA_VERSION}"
        )

    report = payload["verification_report"]
    if not isinstance(report, dict):
        raise ValueError("verification_report must be an object")
    _validate_exact_keys(report, _REPORT_FIELDS, "verification_report")

    summary = report["summary"]
    if not isinstance(summary, str):
        raise ValueError("verification_report.summary must be a string")

    critical_errors = _validate_findings(
        report["critical_errors"], "verification_report.critical_errors"
    )
    gaps = _validate_findings(report["gaps"], "verification_report.gaps")

    verification_status = payload["verification_status"]
    if not isinstance(verification_status, str):
        raise ValueError("verification_status must be a string")
    if verification_status not in _VERIFICATION_STATUSES:
        raise ValueError(
            "verification_status must be 'final' or 'needs_context'"
        )

    verdict = payload["verdict"]
    if not isinstance(verdict, str):
        raise ValueError("verdict must be a string")
    if verdict not in _VERDICTS:
        raise ValueError("verdict must be 'correct' or 'wrong'")

    repair_hints = payload["repair_hints"]
    if not isinstance(repair_hints, str):
        raise ValueError("repair_hints must be a string")

    expansion_requests = _validate_expansion_requests(
        payload["needs_expanded_proofs"]
    )

    checked_item_ids = _validate_checked_item_ids(
        payload["checked_item_ids"], "checked_item_ids"
    )

    proof_digest = payload["proof_digest"]
    if not isinstance(proof_digest, str):
        raise ValueError("proof_digest must be a string")
    if not proof_digest:
        raise ValueError("proof_digest must be non-empty")

    context_digest = payload["context_digest"]
    if not isinstance(context_digest, str):
        raise ValueError("context_digest must be a string")
    if not context_digest:
        raise ValueError("context_digest must be non-empty")

    expected_ids = _normalize_expected_checked_item_ids(expected_checked_item_ids)
    expected_proof = _validate_expected_digest(
        expected_proof_digest, "expected_proof_digest"
    )
    expected_context = _validate_expected_digest(
        expected_context_digest, "expected_context_digest"
    )

    if checked_item_ids != expected_ids:
        raise ValueError(
            "checked_item_ids must exactly match expected_checked_item_ids in order"
        )
    if proof_digest != expected_proof:
        raise ValueError("proof_digest does not match expected_proof_digest")
    if context_digest != expected_context:
        raise ValueError("context_digest does not match expected_context_digest")

    has_findings = bool(critical_errors or gaps)
    if verification_status == "needs_context":
        if verdict != "wrong":
            raise ValueError(
                "verdict must be 'wrong' when verification_status is 'needs_context'"
            )
        if not expansion_requests:
            raise ValueError(
                "needs_expanded_proofs must be non-empty when "
                "verification_status is 'needs_context'"
            )
        if has_findings:
            raise ValueError(
                "needs_context is a protocol request and must not contain findings"
            )
        if repair_hints != "":
            raise ValueError(
                "repair_hints must be empty when verification_status is 'needs_context'"
            )
        return deepcopy(payload)

    if expansion_requests:
        raise ValueError(
            "needs_expanded_proofs must be empty when verification_status is 'final'"
        )
    if verdict == "correct":
        if has_findings:
            raise ValueError(
                "verdict must be 'wrong' when critical_errors or gaps are non-empty"
            )
        if repair_hints != "":
            raise ValueError("repair_hints must be empty when verdict is 'correct'")
    else:
        if not has_findings:
            raise ValueError(
                "verdict must be 'correct' when critical_errors and gaps are empty"
            )
        if not repair_hints.strip():
            raise ValueError(
                "repair_hints must contain non-whitespace text when verdict is 'wrong'"
            )

    return deepcopy(payload)


def build_verification_output(
    *,
    verification_report: Mapping[str, Any],
    repair_hints: str,
    checked_item_ids: Sequence[str],
    proof_digest: str,
    context_digest: str,
    verification_status: str = "final",
    needs_expanded_proofs: Sequence[Mapping[str, str]] = (),
) -> dict[str, Any]:
    """Build validated output, deriving the verdict solely from the report."""

    report = deepcopy(verification_report)
    normalized_ids = _normalize_expected_checked_item_ids(checked_item_ids)
    has_findings = bool(
        isinstance(report, Mapping)
        and (report.get("critical_errors") or report.get("gaps"))
    )
    payload = {
        "output_schema_version": OUTPUT_SCHEMA_VERSION,
        "verification_report": report,
        "verification_status": verification_status,
        "verdict": (
            "wrong"
            if verification_status == "needs_context" or has_findings
            else "correct"
        ),
        "repair_hints": repair_hints,
        "needs_expanded_proofs": deepcopy(list(needs_expanded_proofs)),
        "checked_item_ids": deepcopy(normalized_ids),
        "proof_digest": proof_digest,
        "context_digest": context_digest,
    }
    return validate_verification_output(
        payload,
        expected_checked_item_ids=normalized_ids,
        expected_proof_digest=proof_digest,
        expected_context_digest=context_digest,
    )


__all__ = [
    "OUTPUT_SCHEMA_VERSION",
    "build_verification_output",
    "validate_verification_output",
]
