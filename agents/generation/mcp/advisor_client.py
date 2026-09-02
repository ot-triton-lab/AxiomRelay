"""Read one owner-imported Chrome advisor receipt as untrusted data.

This module has no browser, model, API, verification, or publication surface.
The caller must supply the digest announced by the durable hot-join ledger, and
the receipt must match the runner-bound run and problem identifiers exactly.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
from typing import Any


RECEIPT_SCHEMA_VERSION = "rethlas-advisor-v1"
LINEAGE_RECEIPT_SCHEMA_VERSION = "rethlas-advisor-v2"
MAX_RECEIPT_BYTES = 800_000
RECEIPT_ID_RE = re.compile(r"^adv_[0-9a-f]{32}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
PROBLEM_ID_RE = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9-])?"
    r"(?:/[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9-])?)*$"
)
EXPECTED_KEYS = {
    "answer",
    "answer_bytes",
    "answer_sha256",
    "authorization_id",
    "billing_basis",
    "completion_evidence",
    "computer_use_skill_sha256",
    "conversation_url_sha256",
    "cost",
    "model",
    "problem_id",
    "query_skill_sha256",
    "question",
    "question_bytes",
    "question_sha256",
    "request_id",
    "run_id",
    "schema_version",
    "source_kind",
    "submitted_at_utc",
    "transport",
    "trust",
    "ui_mode",
    "usage",
}
LINEAGE_EXPECTED_KEYS = EXPECTED_KEYS | {"lineage"}
NO_AUTHORITY = {
    "citation_authority": False,
    "instruction_authority": False,
    "mathematical_authority": False,
    "verification_authority": False,
}
EXTERNAL_LINEAGE_ACK = (
    "I acknowledge that this external conversation lineage is owner-asserted, "
    "not locally verified by AxiomRelay, and grants no mathematical, instruction, "
    "verification, publication, or browser-dispatch authority."
)


class AdvisorReceiptError(RuntimeError):
    """The requested advisor receipt failed its local integrity contract."""


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise AdvisorReceiptError("advisor receipt contains a duplicate JSON key")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> Any:
    raise AdvisorReceiptError(f"advisor receipt contains invalid JSON number {value}")


def _regular_owner_only_directory(path: Path) -> Path:
    absolute = Path(os.path.abspath(os.fspath(path)))
    try:
        metadata = absolute.lstat()
    except OSError as exc:
        raise AdvisorReceiptError("advisor receipt root is unavailable") from exc
    if not stat.S_ISDIR(metadata.st_mode) or absolute.is_symlink():
        raise AdvisorReceiptError("advisor receipt root must be a real directory")
    if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
        raise AdvisorReceiptError("advisor receipt root has another owner")
    if stat.S_IMODE(metadata.st_mode) & 0o077:
        raise AdvisorReceiptError("advisor receipt root must be owner-only")
    return absolute


def _bounded_receipt_bytes(root: Path, receipt_id: str) -> bytes:
    directory_flags = (
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        before = root.lstat()
        root_fd = os.open(root, directory_flags)
        opened = os.fstat(root_fd)
    except OSError as exc:
        raise AdvisorReceiptError(
            "advisor receipt root cannot be opened safely"
        ) from exc
    if (
        not stat.S_ISDIR(before.st_mode)
        or not stat.S_ISDIR(opened.st_mode)
        or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
        or (hasattr(os, "getuid") and opened.st_uid != os.getuid())
        or stat.S_IMODE(opened.st_mode) & 0o077
    ):
        os.close(root_fd)
        raise AdvisorReceiptError("advisor receipt root changed during secure open")
    descriptor = -1
    try:
        descriptor = os.open(
            f"{receipt_id}.report.json",
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=root_fd,
        )
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise AdvisorReceiptError("advisor receipt must be a regular file")
        if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
            raise AdvisorReceiptError("advisor receipt has another owner")
        if stat.S_IMODE(metadata.st_mode) & 0o077:
            raise AdvisorReceiptError("advisor receipt must be owner-only")
        if metadata.st_nlink != 1:
            raise AdvisorReceiptError("advisor receipt must have exactly one link")
        if metadata.st_size > MAX_RECEIPT_BYTES:
            raise AdvisorReceiptError("advisor receipt exceeds its size limit")
        chunks: list[bytes] = []
        remaining = int(metadata.st_size)
        while remaining:
            chunk = os.read(descriptor, min(65_536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) != metadata.st_size or os.read(descriptor, 1):
            raise AdvisorReceiptError("advisor receipt changed during secure read")
        return raw
    except OSError as exc:
        raise AdvisorReceiptError("advisor receipt cannot be read safely") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(root_fd)


def _text_digest_field(receipt: dict[str, Any], name: str) -> str:
    value = receipt.get(name)
    if not isinstance(value, str) or SHA256_RE.fullmatch(value) is None:
        raise AdvisorReceiptError(f"advisor receipt has invalid {name}")
    return value


def _validated_lineage(receipt: dict[str, Any]) -> dict[str, Any] | None:
    schema = receipt.get("schema_version")
    if schema == RECEIPT_SCHEMA_VERSION:
        if set(receipt) != EXPECTED_KEYS:
            raise AdvisorReceiptError("advisor receipt has an unsupported shape")
        return None
    if (
        schema != LINEAGE_RECEIPT_SCHEMA_VERSION
        or set(receipt) != LINEAGE_EXPECTED_KEYS
    ):
        raise AdvisorReceiptError("advisor receipt has an unsupported version or shape")
    lineage = receipt.get("lineage")
    if not isinstance(lineage, dict):
        raise AdvisorReceiptError("advisor receipt lineage is invalid")
    common = {
        "conversation_url_sha256",
        "grants_authority",
        "kind",
        "lineage_depth",
        "lineage_root_request_id",
        "locally_verified",
    }
    kind = lineage.get("kind")
    if kind == "rethlas_predecessor":
        expected = common | {
            "predecessor_receipt_sha256",
            "predecessor_request_id",
            "predecessor_state_at_prepare",
        }
        if (
            set(lineage) != expected
            or lineage.get("locally_verified") is not True
            or lineage.get("predecessor_state_at_prepare")
            not in {"completed", "imported"}
            or not isinstance(lineage.get("predecessor_request_id"), str)
            or RECEIPT_ID_RE.fullmatch(lineage["predecessor_request_id"]) is None
        ):
            raise AdvisorReceiptError("local advisor lineage is invalid")
        _text_digest_field(lineage, "predecessor_receipt_sha256")
    elif kind == "owner_asserted_external":
        expected = common | {
            "external_receipt_sha256",
            "external_request_id",
            "owner_acknowledgement",
            "source_context_sha256",
            "source_repo",
        }
        if (
            set(lineage) != expected
            or lineage.get("locally_verified") is not False
            or lineage.get("source_repo") != "Danus"
            or lineage.get("lineage_depth") != 1
            or lineage.get("lineage_root_request_id") != receipt.get("request_id")
            or not isinstance(lineage.get("external_request_id"), str)
            or not lineage["external_request_id"]
            or len(lineage["external_request_id"].encode("utf-8")) > 256
            or any(
                ord(character) < 0x20 for character in lineage["external_request_id"]
            )
            or lineage.get("owner_acknowledgement") != EXTERNAL_LINEAGE_ACK
        ):
            raise AdvisorReceiptError("external advisor lineage is invalid")
        _text_digest_field(lineage, "external_receipt_sha256")
        _text_digest_field(lineage, "source_context_sha256")
    else:
        raise AdvisorReceiptError("advisor receipt lineage kind is invalid")
    if (
        lineage.get("grants_authority") is not False
        or not isinstance(lineage.get("lineage_depth"), int)
        or isinstance(lineage.get("lineage_depth"), bool)
        or lineage["lineage_depth"] <= 0
        or not isinstance(lineage.get("lineage_root_request_id"), str)
        or RECEIPT_ID_RE.fullmatch(lineage["lineage_root_request_id"]) is None
    ):
        raise AdvisorReceiptError("advisor lineage authority/root fields are invalid")
    _text_digest_field(lineage, "conversation_url_sha256")
    if lineage["conversation_url_sha256"] != receipt.get("conversation_url_sha256"):
        raise AdvisorReceiptError("advisor lineage conversation digest mismatch")
    return dict(lineage)


def advisor_report_get(
    *,
    problem_id: str,
    run_id: str,
    receipt_id: str,
    expected_receipt_sha256: str,
) -> dict[str, Any]:
    """Return a content-addressed advisor report with all authority flags false."""

    if PROBLEM_ID_RE.fullmatch(problem_id) is None:
        raise ValueError("problem_id must be a normalized data-relative identifier")
    if RUN_ID_RE.fullmatch(run_id) is None:
        raise ValueError("run_id has an unsafe shape")
    if RECEIPT_ID_RE.fullmatch(receipt_id) is None:
        raise ValueError("receipt_id has an unsafe shape")
    if SHA256_RE.fullmatch(expected_receipt_sha256) is None:
        raise ValueError("expected_receipt_sha256 must be lowercase SHA-256 hex")

    expected_problem = os.environ.get("RETHLAS_EXPECTED_PROBLEM_ID")
    expected_run = os.environ.get("RETHLAS_EXPECTED_HOTJOIN_RUN_ID")
    configured_root = os.environ.get("RETHLAS_ADVISOR_RECEIPTS_ROOT")
    if not expected_problem or not expected_run or not configured_root:
        raise AdvisorReceiptError("advisor receipt access is not enabled by the runner")
    if problem_id != expected_problem or run_id != expected_run:
        raise AdvisorReceiptError("advisor receipt request is outside the bound run")

    root = _regular_owner_only_directory(Path(configured_root))
    raw = _bounded_receipt_bytes(root, receipt_id)
    actual_digest = hashlib.sha256(raw).hexdigest()
    if actual_digest != expected_receipt_sha256:
        raise AdvisorReceiptError("advisor receipt digest does not match the notice")
    try:
        decoded = raw.decode("utf-8", errors="strict")
        receipt = json.loads(
            decoded,
            object_pairs_hook=_strict_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise AdvisorReceiptError("advisor receipt is not strict UTF-8 JSON") from exc
    if not isinstance(receipt, dict):
        raise AdvisorReceiptError("advisor receipt has an unsupported shape")
    lineage = _validated_lineage(receipt)
    if receipt["request_id"] != receipt_id:
        raise AdvisorReceiptError("advisor receipt id mismatch")
    if receipt["run_id"] != run_id or receipt["problem_id"] != problem_id:
        raise AdvisorReceiptError("advisor receipt run binding mismatch")
    if receipt["source_kind"] != "advisor":
        raise AdvisorReceiptError("advisor receipt source provenance mismatch")
    if receipt["transport"] != "chatgpt_pro_browser" or receipt["ui_mode"] != "Pro":
        raise AdvisorReceiptError("advisor receipt transport provenance mismatch")
    if (
        receipt["model"] is not None
        or receipt["usage"] is not None
        or receipt["cost"] is not None
        or receipt["billing_basis"] != "subscription"
    ):
        raise AdvisorReceiptError("advisor receipt billing provenance is invalid")
    if receipt["trust"] != NO_AUTHORITY:
        raise AdvisorReceiptError("advisor receipt authority flags must all be false")
    canonical = (
        json.dumps(
            receipt,
            allow_nan=False,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    if raw != canonical:
        raise AdvisorReceiptError("advisor receipt is not canonical broker JSON")

    question = receipt["question"]
    answer = receipt["answer"]
    if not isinstance(question, str) or not isinstance(answer, str):
        raise AdvisorReceiptError("advisor receipt text fields are invalid")
    try:
        question_raw = question.encode("utf-8", errors="strict")
        answer_raw = answer.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise AdvisorReceiptError("advisor receipt text is not valid UTF-8") from exc
    if (
        not isinstance(receipt["question_bytes"], int)
        or isinstance(receipt["question_bytes"], bool)
        or receipt["question_bytes"] != len(question_raw)
    ):
        raise AdvisorReceiptError("advisor question byte count mismatch")
    if (
        not isinstance(receipt["answer_bytes"], int)
        or isinstance(receipt["answer_bytes"], bool)
        or receipt["answer_bytes"] != len(answer_raw)
    ):
        raise AdvisorReceiptError("advisor answer byte count mismatch")
    if (
        _text_digest_field(receipt, "question_sha256")
        != hashlib.sha256(question_raw).hexdigest()
    ):
        raise AdvisorReceiptError("advisor question digest mismatch")
    if (
        _text_digest_field(receipt, "answer_sha256")
        != hashlib.sha256(answer_raw).hexdigest()
    ):
        raise AdvisorReceiptError("advisor answer digest mismatch")
    for field in (
        "computer_use_skill_sha256",
        "conversation_url_sha256",
        "query_skill_sha256",
    ):
        _text_digest_field(receipt, field)
    evidence = receipt["completion_evidence"]
    if not isinstance(evidence, dict) or evidence != {
        "answer_stable_twice": True,
        "composer_available": True,
        "response_actions_present": True,
        "stable_answer_sha256": receipt["answer_sha256"],
        "working_indicators_absent": True,
    }:
        raise AdvisorReceiptError("advisor completion evidence is invalid")
    if (
        not isinstance(receipt["authorization_id"], str)
        or not receipt["authorization_id"]
    ):
        raise AdvisorReceiptError("advisor authorization provenance is missing")

    return {
        "answer_sha256": receipt["answer_sha256"],
        "authorization_id": receipt["authorization_id"],
        "conversation_url_sha256": receipt["conversation_url_sha256"],
        "lineage": lineage,
        "problem_id": problem_id,
        "question": question,
        "question_sha256": receipt["question_sha256"],
        "receipt_id": receipt_id,
        "receipt_sha256": actual_digest,
        "report_text": answer,
        "run_id": run_id,
        "schema_version": receipt["schema_version"],
        "source_kind": "advisor",
        "transport": "chatgpt_pro_browser",
        "trust": dict(NO_AUTHORITY),
        "untrusted_data": True,
    }
