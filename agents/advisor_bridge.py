#!/usr/bin/env python3
"""Owner-side durable bridge for Chrome-only ChatGPT Pro advisor reports.

This module never opens Chrome, invokes a model, or uses an API.  It records
the exact owner-authorized question before a separate Codex task follows the
``query-chatgpt-pro`` skill, then imports completed, content-addressed advisor
receipts into the AxiomRelay hot-join control plane. Every consultation or
follow-up is a separate request id with a separately authorized exact question.

The database and receipts live beside (not inside) ``agents/generation`` so a
generation model cannot forge advisor provenance.  Browser page content is
always untrusted mathematical data: it has no instruction, citation,
verification, or publication authority.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import sys
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit
import uuid


SCHEMA_VERSION = 5
RECEIPT_SCHEMA_VERSION = "rethlas-advisor-v1"
LINEAGE_RECEIPT_SCHEMA_VERSION = "rethlas-advisor-v2"
COMPLETION_RECEIPT_SCHEMA_VERSION = "rethlas-advisor-completion-v1"
LINEAGE_COMPLETION_RECEIPT_SCHEMA_VERSION = "rethlas-advisor-completion-v2"
FAILED_NOT_SUBMITTED_RECEIPT_SCHEMA_VERSION = "rethlas-advisor-failed-not-submitted-v1"
LINEAGE_FAILED_NOT_SUBMITTED_RECEIPT_SCHEMA_VERSION = (
    "rethlas-advisor-failed-not-submitted-v2"
)
NEEDS_USER_INPUT_RECEIPT_SCHEMA_VERSION = "rethlas-advisor-needs-user-input-v1"
LINEAGE_NEEDS_USER_INPUT_RECEIPT_SCHEMA_VERSION = "rethlas-advisor-needs-user-input-v2"
ABANDONED_UNKNOWN_RECEIPT_SCHEMA_VERSION = (
    "rethlas-advisor-owner-abandoned-outcome-unknown-v1"
)
LINEAGE_ABANDONED_UNKNOWN_RECEIPT_SCHEMA_VERSION = (
    "rethlas-advisor-owner-abandoned-outcome-unknown-v2"
)
SOURCE_KIND = "advisor"
TRANSPORT = "chatgpt_pro_browser"
UI_MODE = "Pro"
NO_AUTHORITY = {
    "citation_authority": False,
    "instruction_authority": False,
    "mathematical_authority": False,
    "verification_authority": False,
}
OUTCOME_UNKNOWN_ACK = (
    "I acknowledge that submission outcome is unknown and will not resubmit "
    "this exact question."
)
EXTERNAL_LINEAGE_ACK = (
    "I acknowledge that this external conversation lineage is owner-asserted, "
    "not locally verified by AxiomRelay, and grants no mathematical, instruction, "
    "verification, publication, or browser-dispatch authority."
)
LINEAGE_KINDS = frozenset({"none", "rethlas_predecessor", "owner_asserted_external"})
EXTERNAL_SOURCE_REPO = "Danus"
STATES = frozenset(
    {
        "prepared",
        "authorized",
        "dispatching",
        "failed_not_submitted",
        "submitted",
        "submission_unknown",
        "completed",
        "needs_user_input",
        "owner_abandoned_outcome_unknown",
        "imported",
        "delivery_unknown",
        "abandoned",
    }
)
RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
PROBLEM_ID_RE = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9-])?"
    r"(?:/[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9-])?)*$"
)
REQUEST_ID_RE = re.compile(r"^adv_[0-9a-f]{32}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
AUTHORIZATION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
EXTERNAL_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
MAX_QUESTION_BYTES = 200_000
MAX_ANSWER_BYTES = 500_000
MAX_CLARIFICATION_BYTES = 65_536
MAX_REASON_CHARS = 4096
MAX_RECEIPT_BYTES = 800_000
ZERO_DIGEST = "0" * 64
DEFAULT_ROOT = Path(__file__).resolve().parent / ".rethlas_advisor"
DEFAULT_DB = DEFAULT_ROOT / "jobs.sqlite3"
DEFAULT_RECEIPTS_ROOT = DEFAULT_ROOT / "receipts"


class AdvisorError(RuntimeError):
    """A durable advisor operation was unsafe or violated its state contract."""


class AdvisorConflict(AdvisorError):
    """An idempotency key was reused with different immutable input."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_text(value: str) -> str:
    return _sha256_bytes(value.encode("utf-8"))


def _validate_sha256(value: str, label: str) -> str:
    if not isinstance(value, str) or SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"{label} must be lowercase SHA-256 hex")
    return value


def _validate_run_id(value: str) -> str:
    if not isinstance(value, str) or RUN_ID_RE.fullmatch(value) is None:
        raise ValueError("run_id has an unsafe shape")
    return value


def _validate_problem_id(value: str) -> str:
    if not isinstance(value, str) or PROBLEM_ID_RE.fullmatch(value) is None:
        raise ValueError("problem_id must be a normalized data-relative identifier")
    return value


def _validate_request_id(value: str) -> str:
    if not isinstance(value, str) or REQUEST_ID_RE.fullmatch(value) is None:
        raise ValueError("request_id must use the adv_<32 lowercase hex> form")
    return value


def _bounded_text(value: str, *, label: str, maximum: int) -> tuple[str, int]:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be non-empty text")
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{label} must be valid UTF-8") from exc
    if len(encoded) > maximum:
        raise ValueError(f"{label} exceeds {maximum} UTF-8 bytes")
    return value, len(encoded)


def _conversation_url_digest(conversation_url: str) -> str:
    if not isinstance(conversation_url, str):
        raise ValueError("conversation URL must be bounded exact text")
    try:
        encoded = conversation_url.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ValueError("conversation URL must be valid UTF-8") from exc
    if (
        not conversation_url
        or conversation_url != conversation_url.strip()
        or len(encoded) > 4096
        or any(ord(character) < 0x20 for character in conversation_url)
    ):
        raise ValueError("conversation URL must be bounded exact text")
    parsed = urlsplit(conversation_url)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("conversation URL has an invalid port") from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname != "chatgpt.com"
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
    ):
        raise ValueError(
            "conversation URL must use https://chatgpt.com/ with no port or port 443"
        )
    return _sha256_bytes(encoded)


def _validate_external_request_id(value: str) -> str:
    if not isinstance(value, str) or EXTERNAL_REQUEST_ID_RE.fullmatch(value) is None:
        raise ValueError("external_request_id has an unsafe shape")
    return value


def _lineage_payload(row: Mapping[str, Any]) -> dict[str, Any] | None:
    """Return the bounded public lineage claim stored in receipts and status."""

    kind = str(row["lineage_kind"])
    if kind == "none":
        return None
    common = {
        "conversation_url_sha256": row["lineage_conversation_url_sha256"],
        "grants_authority": False,
        "kind": kind,
        "lineage_depth": row["lineage_depth"],
        "lineage_root_request_id": row["lineage_root_request_id"],
    }
    if kind == "rethlas_predecessor":
        return {
            **common,
            "locally_verified": True,
            "predecessor_receipt_sha256": row["predecessor_receipt_sha256"],
            "predecessor_request_id": row["predecessor_request_id"],
            "predecessor_state_at_prepare": row["predecessor_state_at_prepare"],
        }
    if kind == "owner_asserted_external":
        return {
            **common,
            "external_receipt_sha256": row["external_receipt_sha256"],
            "external_request_id": row["external_request_id"],
            "locally_verified": False,
            "owner_acknowledgement": row["external_owner_ack"],
            "source_context_sha256": row["external_source_context_sha256"],
            "source_repo": row["external_source_repo"],
        }
    raise AdvisorError("advisor lineage kind is invalid")


def _attach_lineage(
    payload: dict[str, Any],
    row: Mapping[str, Any],
    *,
    lineage_schema_version: str,
) -> dict[str, Any]:
    lineage = _lineage_payload(row)
    if lineage is not None:
        payload["lineage"] = lineage
        payload["schema_version"] = lineage_schema_version
    return payload


def _redacted_reason(value: str) -> str:
    bounded = str(value)[:32_768]
    bounded = re.sub(
        r"(?i)https://chatgpt\.com(?::[0-9]+)?(?:[/?#][^\s<>\"'\\]*)?",
        "https://chatgpt.com/<redacted-conversation>",
        bounded,
    )
    bounded = re.sub(
        r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+",
        "Bearer <redacted>",
        bounded,
    )
    bounded = re.sub(
        r"(?i)((?:authorization|api[_-]?key|token|secret|password)\s*[=:]\s*)"
        r"([^\s,;]+)",
        r"\1<redacted>",
        bounded,
    )
    bounded = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}\b", "sk-<redacted>", bounded)
    return bounded[:MAX_REASON_CHARS]


def _secure_directory(path: Path) -> Path:
    absolute = Path(os.path.abspath(os.fspath(path)))
    existing: list[Path] = []
    cursor = absolute
    while cursor != cursor.parent:
        existing.append(cursor)
        cursor = cursor.parent
    existing.append(cursor)
    for component in reversed(existing):
        if component.exists() or component.is_symlink():
            if component.is_symlink():
                raise AdvisorError(f"advisor path traverses a symlink: {component}")
    existed = absolute.exists()
    absolute.mkdir(parents=True, mode=0o700, exist_ok=True)
    metadata = absolute.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or absolute.is_symlink():
        raise AdvisorError(f"advisor path is not a real directory: {absolute}")
    if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
        raise AdvisorError(f"advisor directory is not owned by this user: {absolute}")
    if existed and stat.S_IMODE(metadata.st_mode) & 0o077:
        raise AdvisorError(f"advisor directory must be owner-only: {absolute}")
    os.chmod(absolute, 0o700)
    return absolute


def _open_secure_directory(
    path: Path, *, expected_identity: tuple[int, int] | None = None
) -> tuple[int, tuple[int, int]]:
    absolute = Path(os.path.abspath(os.fspath(path)))
    try:
        before = absolute.lstat()
        descriptor = os.open(
            absolute,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        after = os.fstat(descriptor)
    except OSError as exc:
        raise AdvisorError("advisor directory cannot be opened safely") from exc
    identity = (int(after.st_dev), int(after.st_ino))
    if (
        not stat.S_ISDIR(before.st_mode)
        or not stat.S_ISDIR(after.st_mode)
        or (before.st_dev, before.st_ino) != identity
        or (hasattr(os, "getuid") and after.st_uid != os.getuid())
        or stat.S_IMODE(after.st_mode) & 0o077
        or (expected_identity is not None and identity != expected_identity)
    ):
        os.close(descriptor)
        raise AdvisorError("advisor directory changed or is not owner-only")
    return descriptor, identity


def _validate_open_regular_file(
    descriptor: int,
    *,
    label: str,
    expected_identity: tuple[int, int] | None = None,
    maximum: int | None = None,
    require_owner_only: bool = True,
) -> tuple[os.stat_result, tuple[int, int]]:
    metadata = os.fstat(descriptor)
    identity = (int(metadata.st_dev), int(metadata.st_ino))
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or (hasattr(os, "getuid") and metadata.st_uid != os.getuid())
        or (require_owner_only and stat.S_IMODE(metadata.st_mode) & 0o077)
        or (expected_identity is not None and identity != expected_identity)
        or (maximum is not None and metadata.st_size > maximum)
    ):
        raise AdvisorError(f"{label} changed or is not a safe owner-only file")
    return metadata, identity


def _secure_database(path: Path) -> Path:
    absolute = Path(os.path.abspath(os.fspath(path)))
    parent = _secure_directory(absolute.parent)
    directory_fd, _ = _open_secure_directory(parent)
    descriptor = -1
    try:
        try:
            descriptor = os.open(
                absolute.name,
                os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=directory_fd,
            )
        except FileNotFoundError:
            try:
                descriptor = os.open(
                    absolute.name,
                    os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                    dir_fd=directory_fd,
                )
            except FileExistsError:
                descriptor = os.open(
                    absolute.name,
                    os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=directory_fd,
                )
        _validate_open_regular_file(
            descriptor,
            label="advisor database",
            require_owner_only=False,
        )
        os.fchmod(descriptor, 0o600)
        os.fsync(descriptor)
    except OSError as exc:
        raise AdvisorError("advisor database cannot be opened safely") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(directory_fd)
    return absolute


def _atomic_write(
    path: Path,
    raw: bytes,
    *,
    expected_parent_identity: tuple[int, int] | None = None,
) -> None:
    parent = _secure_directory(path.parent)
    directory_fd, _ = _open_secure_directory(
        parent, expected_identity=expected_parent_identity
    )
    temp_name = f".{path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex}"
    descriptor = -1
    try:
        descriptor = os.open(
            temp_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=directory_fd,
        )
        try:
            offset = 0
            while offset < len(raw):
                offset += os.write(descriptor, raw[offset:])
            os.fchmod(descriptor, 0o600)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
            descriptor = -1
        os.link(
            temp_name,
            path.name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
            follow_symlinks=False,
        )
        os.unlink(temp_name, dir_fd=directory_fd)
        os.fsync(directory_fd)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temp_name, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        os.close(directory_fd)


def _existing_receipt_bytes(
    path: Path, *, expected_parent_identity: tuple[int, int] | None = None
) -> bytes:
    directory_fd, _ = _open_secure_directory(
        path.parent, expected_identity=expected_parent_identity
    )
    descriptor = -1
    try:
        try:
            descriptor = os.open(
                path.name,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=directory_fd,
            )
            metadata, _ = _validate_open_regular_file(
                descriptor,
                label="advisor receipt",
                maximum=MAX_RECEIPT_BYTES,
            )
        except OSError as exc:
            raise AdvisorError(
                "advisor receipt must be an owner-only regular non-symlink file"
            ) from exc
        chunks: list[bytes] = []
        remaining = int(metadata.st_size)
        while remaining:
            chunk = os.read(descriptor, min(remaining, 65_536))
            if not chunk:
                raise AdvisorError("advisor receipt changed during secure read")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise AdvisorError("advisor receipt changed during secure read")
        return b"".join(chunks)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(directory_fd)


def _strict_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise AdvisorError("advisor receipt contains a duplicate JSON key")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> Any:
    raise AdvisorError(f"advisor receipt contains invalid JSON number {value}")


def _completion_receipt_payload(
    row: Mapping[str, Any],
    *,
    answer_bytes: int,
    answer_sha256: str,
) -> dict[str, Any]:
    return _attach_lineage(
        {
            "answer_bytes": answer_bytes,
            "answer_plaintext_persisted": False,
            "answer_sha256": answer_sha256,
            "authorization_id": row["authorization_id"],
            "billing_basis": "subscription",
            "completion_evidence": {
                "answer_stable_twice": True,
                "composer_available": True,
                "response_actions_present": True,
                "stable_answer_sha256": answer_sha256,
                "working_indicators_absent": True,
            },
            "computer_use_skill_sha256": row["computer_use_skill_sha256"],
            "conversation_url_sha256": row["conversation_url_sha256"],
            "cost": None,
            "model": None,
            "problem_id": row["problem_id"],
            "query_skill_sha256": row["query_skill_sha256"],
            "question": row["question"],
            "question_bytes": row["question_bytes"],
            "question_sha256": row["question_sha256"],
            "request_id": row["request_id"],
            "run_id": row["run_id"],
            "outcome": "completed_answer_committed",
            "schema_version": COMPLETION_RECEIPT_SCHEMA_VERSION,
            "source_kind": SOURCE_KIND,
            "submitted_at_utc": row["submitted_at_utc"],
            "transport": TRANSPORT,
            "trust": dict(NO_AUTHORITY),
            "ui_mode": UI_MODE,
            "usage": None,
        },
        row,
        lineage_schema_version=LINEAGE_COMPLETION_RECEIPT_SCHEMA_VERSION,
    )


def _report_receipt_payload(
    row: Mapping[str, Any],
    *,
    answer: str,
    answer_bytes: int,
    answer_sha256: str,
) -> dict[str, Any]:
    receipt = _completion_receipt_payload(
        row,
        answer_bytes=answer_bytes,
        answer_sha256=answer_sha256,
    )
    receipt.pop("answer_plaintext_persisted")
    receipt.pop("outcome")
    receipt["answer"] = answer
    receipt["schema_version"] = (
        LINEAGE_RECEIPT_SCHEMA_VERSION
        if _lineage_payload(row) is not None
        else RECEIPT_SCHEMA_VERSION
    )
    return receipt


def _failed_not_submitted_receipt_payload(
    row: Mapping[str, Any], *, reason: str, prior_state: str
) -> dict[str, Any]:
    return _attach_lineage(
        {
            "authorization_id": row["authorization_id"],
            "browser_submission_possible": False,
            "computer_use_skill_sha256": row["computer_use_skill_sha256"],
            "dispatch_count": row["dispatch_count"],
            "prior_state": prior_state,
            "outcome": "failed_not_submitted",
            "problem_id": row["problem_id"],
            "query_skill_sha256": row["query_skill_sha256"],
            "question_sha256": row["question_sha256"],
            "reason": reason,
            "request_id": row["request_id"],
            "run_id": row["run_id"],
            "schema_version": FAILED_NOT_SUBMITTED_RECEIPT_SCHEMA_VERSION,
            "send_clicked": False,
            "source_kind": SOURCE_KIND,
            "transport": TRANSPORT,
            "ui_mode": UI_MODE,
        },
        row,
        lineage_schema_version=LINEAGE_FAILED_NOT_SUBMITTED_RECEIPT_SCHEMA_VERSION,
    )


def _needs_user_input_receipt_payload(
    row: Mapping[str, Any],
    *,
    clarification_bytes: int,
    clarification_sha256: str,
) -> dict[str, Any]:
    return _attach_lineage(
        {
            "authorization_id": row["authorization_id"],
            "automatic_followup_allowed": False,
            "clarification_bytes": clarification_bytes,
            "clarification_plaintext_persisted": False,
            "clarification_sha256": clarification_sha256,
            "conversation_url_sha256": row["conversation_url_sha256"],
            "outcome": "needs_user_input",
            "problem_id": row["problem_id"],
            "question_sha256": row["question_sha256"],
            "request_id": row["request_id"],
            "run_id": row["run_id"],
            "schema_version": NEEDS_USER_INPUT_RECEIPT_SCHEMA_VERSION,
            "source_kind": SOURCE_KIND,
            "transport": TRANSPORT,
            "ui_mode": UI_MODE,
        },
        row,
        lineage_schema_version=LINEAGE_NEEDS_USER_INPUT_RECEIPT_SCHEMA_VERSION,
    )


def _abandoned_unknown_receipt_payload(
    row: Mapping[str, Any], *, reason: str
) -> dict[str, Any]:
    return _attach_lineage(
        {
            "acknowledgement": OUTCOME_UNKNOWN_ACK,
            "authorization_id": row["authorization_id"],
            "conversation_url_sha256": row["conversation_url_sha256"],
            "outcome": "owner_abandoned_outcome_unknown",
            "problem_id": row["problem_id"],
            "question_sha256": row["question_sha256"],
            "reason": reason,
            "request_id": row["request_id"],
            "run_id": row["run_id"],
            "schema_version": ABANDONED_UNKNOWN_RECEIPT_SCHEMA_VERSION,
            "source_kind": SOURCE_KIND,
            "submission_may_have_occurred": True,
            "transport": TRANSPORT,
            "ui_mode": UI_MODE,
        },
        row,
        lineage_schema_version=LINEAGE_ABANDONED_UNKNOWN_RECEIPT_SCHEMA_VERSION,
    )


class AdvisorLedger:
    """Owner-only state machine and hash-chained audit log for advisor jobs."""

    def __init__(
        self,
        path: Path | str = DEFAULT_DB,
        *,
        receipts_root: Path | str = DEFAULT_RECEIPTS_ROOT,
    ) -> None:
        self.path = _secure_database(Path(path))
        self.receipts_root = _secure_directory(Path(receipts_root))
        database_parent_fd, self._database_parent_identity = _open_secure_directory(
            self.path.parent
        )
        database_fd = -1
        try:
            database_fd = os.open(
                self.path.name,
                os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=database_parent_fd,
            )
            _, self._database_identity = _validate_open_regular_file(
                database_fd, label="advisor database"
            )
        finally:
            if database_fd >= 0:
                os.close(database_fd)
            os.close(database_parent_fd)
        receipts_fd, self._receipts_root_identity = _open_secure_directory(
            self.receipts_root
        )
        os.close(receipts_fd)
        self._initialize()

    @staticmethod
    def _owner_uid() -> int:
        return os.getuid() if hasattr(os, "getuid") else 0

    def _connect(self) -> sqlite3.Connection:
        directory_fd, _ = _open_secure_directory(
            self.path.parent, expected_identity=self._database_parent_identity
        )
        descriptor = -1
        connection: sqlite3.Connection | None = None
        try:
            descriptor = os.open(
                self.path.name,
                os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=directory_fd,
            )
            _validate_open_regular_file(
                descriptor,
                label="advisor database",
                expected_identity=self._database_identity,
            )
            connection = sqlite3.connect(self.path, timeout=10.0, isolation_level=None)
            confirmation_fd = os.open(
                self.path.name,
                os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=directory_fd,
            )
            try:
                _validate_open_regular_file(
                    confirmation_fd,
                    label="advisor database",
                    expected_identity=self._database_identity,
                )
            finally:
                os.close(confirmation_fd)
        except (OSError, sqlite3.Error) as exc:
            if connection is not None:
                connection.close()
            raise AdvisorError("advisor database changed during secure open") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            os.close(directory_fd)
        assert connection is not None
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    def _existing_receipt_bytes(self, path: Path) -> bytes:
        return _existing_receipt_bytes(
            path, expected_parent_identity=self._receipts_root_identity
        )

    def _atomic_write(self, path: Path, raw: bytes) -> None:
        _atomic_write(
            path,
            raw,
            expected_parent_identity=self._receipts_root_identity,
        )

    def _receipt_exists(self, path: Path) -> bool:
        directory_fd, _ = _open_secure_directory(
            path.parent, expected_identity=self._receipts_root_identity
        )
        try:
            try:
                os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
            except FileNotFoundError:
                return False
            return True
        finally:
            os.close(directory_fd)

    def _discard_uncommitted_terminal_receipt(self, request_id: str) -> None:
        """Remove only an orphan that has no committed ledger digest.

        Version-4 code could publish ``<request_id>.json`` and then roll back
        SQLite.  The nonterminal row is authoritative evidence that such bytes
        never became a terminal receipt.  Validate the inode before unlinking so
        a symlink or special-file substitution still fails closed.
        """

        request_id = _validate_request_id(request_id)
        path = self.receipts_root / f"{request_id}.json"
        directory_fd, _ = _open_secure_directory(
            path.parent, expected_identity=self._receipts_root_identity
        )
        descriptor = -1
        try:
            try:
                descriptor = os.open(
                    path.name,
                    os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=directory_fd,
                )
            except FileNotFoundError:
                return
            metadata, identity = _validate_open_regular_file(
                descriptor,
                label="uncommitted advisor receipt",
                require_owner_only=True,
            )
            visible = os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
            if (
                metadata.st_nlink != 1
                or identity != (int(visible.st_dev), int(visible.st_ino))
            ):
                raise AdvisorError("uncommitted advisor receipt changed before cleanup")
            os.unlink(path.name, dir_fd=directory_fd)
            os.fsync(directory_fd)
        except OSError as exc:
            raise AdvisorError(
                "uncommitted advisor receipt must be an owner-only regular "
                "non-symlink file"
            ) from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            os.close(directory_fd)

    @staticmethod
    def _terminal_receipt_raw(row: Mapping[str, Any]) -> bytes | None:
        state = str(row["state"])
        if state in {"completed", "delivery_unknown", "imported"}:
            answer_bytes = row["answer_bytes"]
            answer_sha256 = row["answer_sha256"]
            if (
                isinstance(answer_bytes, bool)
                or not isinstance(answer_bytes, int)
                or answer_bytes <= 0
                or not isinstance(answer_sha256, str)
                or SHA256_RE.fullmatch(answer_sha256) is None
            ):
                raise AdvisorError("completed advisor receipt fields are incomplete")
            payload = _completion_receipt_payload(
                row,
                answer_bytes=answer_bytes,
                answer_sha256=answer_sha256,
            )
        elif state == "failed_not_submitted":
            dispatch_count = row["dispatch_count"]
            reason = row["terminal_reason"]
            if dispatch_count not in {0, 1} or not isinstance(reason, str):
                raise AdvisorError(
                    "failed_not_submitted terminal ledger fields are invalid"
                )
            payload = _failed_not_submitted_receipt_payload(
                row,
                reason=reason,
                prior_state="dispatching" if dispatch_count == 1 else "authorized",
            )
        elif state == "needs_user_input":
            clarification_bytes = row["clarification_bytes"]
            clarification_sha256 = row["clarification_sha256"]
            if (
                isinstance(clarification_bytes, bool)
                or not isinstance(clarification_bytes, int)
                or clarification_bytes <= 0
                or not isinstance(clarification_sha256, str)
                or SHA256_RE.fullmatch(clarification_sha256) is None
            ):
                raise AdvisorError(
                    "needs_user_input terminal ledger fields are invalid"
                )
            payload = _needs_user_input_receipt_payload(
                row,
                clarification_bytes=clarification_bytes,
                clarification_sha256=clarification_sha256,
            )
        elif state == "owner_abandoned_outcome_unknown":
            reason = row["terminal_reason"]
            if row["outcome_unknown_abandoned"] != 1 or not isinstance(reason, str):
                raise AdvisorError("unknown-abandon terminal ledger fields are invalid")
            payload = _abandoned_unknown_receipt_payload(row, reason=reason)
        else:
            return None
        raw = (_canonical_json(payload) + "\n").encode("utf-8")
        if len(raw) > MAX_RECEIPT_BYTES:
            raise AdvisorError("advisor receipt exceeds its durable size limit")
        return raw

    def _publish_pending_terminal_receipt(self, request_id: str) -> None:
        """Finish the recoverable filesystem half of a committed terminal."""

        request_id = _validate_request_id(request_id)
        with self._connect() as connection:
            row = self._job(connection, request_id)
            self._attest_lineage(connection, row)
            raw = self._terminal_receipt_raw(row)
            if raw is None or int(row["receipt_published"]) == 1:
                return
            expected_sha256 = _sha256_bytes(raw)
            if row["receipt_sha256"] != expected_sha256:
                raise AdvisorConflict(
                    "pending terminal receipt differs from its committed digest"
                )

        receipt_path = self.receipts_root / f"{request_id}.json"
        if self._receipt_exists(receipt_path):
            if self._existing_receipt_bytes(receipt_path) != raw:
                raise AdvisorConflict(
                    "pending terminal receipt path contains different bytes"
                )
        else:
            try:
                self._atomic_write(receipt_path, raw)
            except FileExistsError:
                if self._existing_receipt_bytes(receipt_path) != raw:
                    raise AdvisorConflict(
                        "concurrent terminal receipt publication used other bytes"
                    )

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = self._job(connection, request_id)
            self._attest_lineage(connection, current)
            current_raw = self._terminal_receipt_raw(current)
            if (
                current_raw != raw
                or current["receipt_sha256"] != expected_sha256
                or int(current["receipt_published"]) not in {0, 1}
            ):
                raise AdvisorConflict(
                    "terminal receipt commitment changed during publication"
                )
            if int(current["receipt_published"]) == 0:
                changed = connection.execute(
                    "UPDATE jobs SET receipt_published = 1, updated_at_utc = ? "
                    "WHERE request_id = ? AND receipt_published = 0",
                    (_utc_now(), request_id),
                ).rowcount
                if changed != 1:
                    raise AdvisorConflict("terminal receipt publication lost its CAS")
            connection.commit()

    def _initialize(self) -> None:
        states = ",".join(f"'{state}'" for state in sorted(STATES))
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                f"""
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                INSERT OR IGNORE INTO metadata(key, value)
                    VALUES ('schema_version', '{SCHEMA_VERSION}');
                INSERT OR IGNORE INTO metadata(key, value)
                    VALUES ('head_digest', '{ZERO_DIGEST}');

                CREATE TABLE IF NOT EXISTS jobs (
                    request_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    problem_id TEXT NOT NULL,
                    owner_uid INTEGER NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ({states})),
                    question TEXT NOT NULL,
                    question_sha256 TEXT NOT NULL,
                    question_bytes INTEGER NOT NULL,
                    query_skill_sha256 TEXT NOT NULL,
                    computer_use_skill_sha256 TEXT NOT NULL,
                    lineage_kind TEXT NOT NULL DEFAULT 'none'
                        CHECK(lineage_kind IN
                            ('none', 'rethlas_predecessor',
                             'owner_asserted_external')),
                    predecessor_request_id TEXT REFERENCES jobs(request_id),
                    predecessor_receipt_sha256 TEXT,
                    predecessor_state_at_prepare TEXT,
                    lineage_root_request_id TEXT NOT NULL,
                    lineage_depth INTEGER NOT NULL DEFAULT 0
                        CHECK(lineage_depth >= 0),
                    lineage_conversation_url_sha256 TEXT,
                    external_source_repo TEXT,
                    external_request_id TEXT,
                    external_receipt_sha256 TEXT,
                    external_source_context_sha256 TEXT,
                    external_owner_ack TEXT,
                    authorization_id TEXT,
                    authorized_at_utc TEXT,
                    dispatch_count INTEGER NOT NULL DEFAULT 0,
                    submitted_at_utc TEXT,
                    conversation_url_sha256 TEXT,
                    answer TEXT,
                    answer_sha256 TEXT,
                    answer_bytes INTEGER,
                    stable_answer_sha256 TEXT,
                    completed_at_utc TEXT,
                    receipt_sha256 TEXT,
                    receipt_published INTEGER NOT NULL DEFAULT 0
                        CHECK(receipt_published IN (0, 1)),
                    report_receipt_sha256 TEXT,
                    clarification TEXT,
                    clarification_bytes INTEGER,
                    clarification_sha256 TEXT,
                    terminal_reason TEXT,
                    outcome_unknown_abandoned INTEGER NOT NULL DEFAULT 0
                        CHECK(outcome_unknown_abandoned IN (0, 1)),
                    delivery_client_message_id TEXT,
                    delivery_mode TEXT,
                    delivery_attempt_count INTEGER NOT NULL DEFAULT 0,
                    created_at_utc TEXT NOT NULL,
                    updated_at_utc TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    request_id TEXT NOT NULL REFERENCES jobs(request_id),
                    kind TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    created_at_utc TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    previous_digest TEXT NOT NULL,
                    digest TEXT NOT NULL UNIQUE
                );
                CREATE INDEX IF NOT EXISTS advisor_events_request_sequence
                    ON events(request_id, sequence);
                CREATE TRIGGER IF NOT EXISTS advisor_events_immutable_update
                BEFORE UPDATE ON events BEGIN
                    SELECT RAISE(ABORT, 'advisor events are append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS advisor_events_immutable_delete
                BEFORE DELETE ON events BEGIN
                    SELECT RAISE(ABORT, 'advisor events are append-only');
                END;
                """
            )
            version = connection.execute(
                "SELECT value FROM metadata WHERE key = 'schema_version'"
            ).fetchone()
            if version is not None and str(version["value"]) == "3":
                columns = {
                    str(row["name"])
                    for row in connection.execute("PRAGMA table_info(jobs)").fetchall()
                }
                migrations = {
                    "lineage_kind": "TEXT NOT NULL DEFAULT 'none'",
                    "predecessor_request_id": "TEXT REFERENCES jobs(request_id)",
                    "predecessor_receipt_sha256": "TEXT",
                    "predecessor_state_at_prepare": "TEXT",
                    "lineage_root_request_id": "TEXT",
                    "lineage_depth": "INTEGER NOT NULL DEFAULT 0",
                    "lineage_conversation_url_sha256": "TEXT",
                    "external_source_repo": "TEXT",
                    "external_request_id": "TEXT",
                    "external_receipt_sha256": "TEXT",
                    "external_source_context_sha256": "TEXT",
                    "external_owner_ack": "TEXT",
                }
                connection.execute("BEGIN IMMEDIATE")
                try:
                    for name, declaration in migrations.items():
                        if name not in columns:
                            connection.execute(
                                f"ALTER TABLE jobs ADD COLUMN {name} {declaration}"
                            )
                    connection.execute(
                        "UPDATE jobs SET lineage_root_request_id = request_id "
                        "WHERE lineage_root_request_id IS NULL"
                    )
                    connection.execute(
                        "UPDATE metadata SET value = ? WHERE key = 'schema_version'",
                        ("4",),
                    )
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise
                version = connection.execute(
                    "SELECT value FROM metadata WHERE key = 'schema_version'"
                ).fetchone()
            if version is not None and str(version["value"]) == "4":
                columns = {
                    str(row["name"])
                    for row in connection.execute("PRAGMA table_info(jobs)").fetchall()
                }
                connection.execute("BEGIN IMMEDIATE")
                try:
                    if "receipt_published" not in columns:
                        connection.execute(
                            "ALTER TABLE jobs ADD COLUMN receipt_published "
                            "INTEGER NOT NULL DEFAULT 0 "
                            "CHECK(receipt_published IN (0, 1))"
                        )
                    # Version-4 terminal rows wrote their receipt before the SQL
                    # commit.  A committed digest therefore denotes an already
                    # published legacy receipt; missing/damaged files still fail
                    # closed in the ordinary verifier.
                    connection.execute(
                        "UPDATE jobs SET receipt_published = 1 "
                        "WHERE receipt_sha256 IS NOT NULL"
                    )
                    connection.execute(
                        "UPDATE metadata SET value = ? WHERE key = 'schema_version'",
                        (str(SCHEMA_VERSION),),
                    )
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise
                version = connection.execute(
                    "SELECT value FROM metadata WHERE key = 'schema_version'"
                ).fetchone()
            if version is None or str(version["value"]) != str(SCHEMA_VERSION):
                raise AdvisorError("unsupported advisor database schema")
            columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(jobs)").fetchall()
            }
            required_columns = {
                "clarification_bytes",
                "external_owner_ack",
                "external_receipt_sha256",
                "external_request_id",
                "external_source_repo",
                "external_source_context_sha256",
                "lineage_conversation_url_sha256",
                "lineage_depth",
                "lineage_kind",
                "lineage_root_request_id",
                "predecessor_receipt_sha256",
                "predecessor_request_id",
                "predecessor_state_at_prepare",
                "report_receipt_sha256",
                "receipt_published",
                "terminal_reason",
            }
            missing_columns = sorted(required_columns - columns)
            if missing_columns:
                raise AdvisorError(
                    "unsupported advisor database schema: missing columns "
                    + ", ".join(missing_columns)
                )
        directory_fd, _ = _open_secure_directory(
            self.path.parent, expected_identity=self._database_parent_identity
        )
        try:
            descriptor = os.open(
                self.path.name,
                os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=directory_fd,
            )
            try:
                _validate_open_regular_file(
                    descriptor,
                    label="advisor database",
                    expected_identity=self._database_identity,
                )
                os.fchmod(descriptor, 0o600)
            finally:
                os.close(descriptor)
        finally:
            os.close(directory_fd)

    def _job(self, connection: sqlite3.Connection, request_id: str) -> sqlite3.Row:
        request_id = _validate_request_id(request_id)
        row = connection.execute(
            "SELECT * FROM jobs WHERE request_id = ?", (request_id,)
        ).fetchone()
        if row is None:
            raise AdvisorError(f"unknown advisor request: {request_id}")
        if int(row["owner_uid"]) != self._owner_uid():
            raise AdvisorError("advisor request belongs to a different local user")
        return row

    def _attest_lineage(
        self,
        connection: sqlite3.Connection,
        row: Mapping[str, Any],
    ) -> None:
        """Fail closed unless durable continuation provenance is coherent."""

        request_id = _validate_request_id(str(row["request_id"]))
        kind = str(row["lineage_kind"])
        if kind not in LINEAGE_KINDS:
            raise AdvisorError("advisor lineage kind is invalid")
        root_id = row["lineage_root_request_id"]
        depth = row["lineage_depth"]
        if (
            not isinstance(root_id, str)
            or REQUEST_ID_RE.fullmatch(root_id) is None
            or not isinstance(depth, int)
            or isinstance(depth, bool)
            or depth < 0
        ):
            raise AdvisorError("advisor lineage root or depth is invalid")

        predecessor_fields = (
            row["predecessor_request_id"],
            row["predecessor_receipt_sha256"],
            row["predecessor_state_at_prepare"],
        )
        external_fields = (
            row["external_source_repo"],
            row["external_request_id"],
            row["external_receipt_sha256"],
            row["external_source_context_sha256"],
            row["external_owner_ack"],
        )
        anchor = row["lineage_conversation_url_sha256"]
        current_conversation = row["conversation_url_sha256"]

        if kind == "none":
            if (
                root_id != request_id
                or depth != 0
                or anchor is not None
                or any(value is not None for value in predecessor_fields)
                or any(value is not None for value in external_fields)
            ):
                raise AdvisorConflict(
                    "new-chat advisor lineage fields are inconsistent"
                )
            self._attest_prepared_lineage_event(connection, row)
            self._attest_authorization_event(connection, row)
            self._attest_dispatch_event(connection, row)
            return

        if (
            not isinstance(anchor, str)
            or SHA256_RE.fullmatch(anchor) is None
            or (current_conversation is not None and current_conversation != anchor)
        ):
            raise AdvisorConflict("continuation conversation binding is inconsistent")

        if kind == "rethlas_predecessor":
            if any(value is not None for value in external_fields):
                raise AdvisorConflict("local advisor lineage contains external fields")
            predecessor_id = _validate_request_id(str(row["predecessor_request_id"]))
            if predecessor_id == request_id:
                raise AdvisorConflict("advisor request cannot precede itself")
            predecessor = self._job(connection, predecessor_id)
            if (
                predecessor["run_id"] != row["run_id"]
                or predecessor["problem_id"] != row["problem_id"]
                or predecessor["state"] not in {"completed", "imported"}
            ):
                raise AdvisorConflict(
                    "local predecessor must remain completed/imported in the same run"
                )
            self._verify_terminal_receipt(predecessor)
            expected_root = predecessor["lineage_root_request_id"]
            expected_depth = int(predecessor["lineage_depth"]) + 1
            if (
                row["predecessor_receipt_sha256"] != predecessor["receipt_sha256"]
                or row["predecessor_state_at_prepare"] not in {"completed", "imported"}
                or anchor != predecessor["conversation_url_sha256"]
                or root_id != expected_root
                or depth != expected_depth
            ):
                raise AdvisorConflict("local predecessor lineage binding changed")
            self._attest_prepared_lineage_event(connection, row)
            self._attest_authorization_event(connection, row)
            self._attest_dispatch_event(connection, row)
            return

        if any(value is not None for value in predecessor_fields):
            raise AdvisorConflict("external advisor lineage contains local fields")
        if (
            root_id != request_id
            or depth != 1
            or row["external_source_repo"] != EXTERNAL_SOURCE_REPO
            or not isinstance(row["external_request_id"], str)
            or EXTERNAL_REQUEST_ID_RE.fullmatch(row["external_request_id"]) is None
            or not isinstance(row["external_receipt_sha256"], str)
            or SHA256_RE.fullmatch(row["external_receipt_sha256"]) is None
            or not isinstance(row["external_source_context_sha256"], str)
            or SHA256_RE.fullmatch(row["external_source_context_sha256"]) is None
            or row["external_owner_ack"] != EXTERNAL_LINEAGE_ACK
        ):
            raise AdvisorConflict("owner-asserted external lineage is inconsistent")
        self._attest_prepared_lineage_event(connection, row)
        self._attest_authorization_event(connection, row)
        self._attest_dispatch_event(connection, row)

    @staticmethod
    def _attest_exact_event(
        connection: sqlite3.Connection,
        row: Mapping[str, Any],
        *,
        kind: str,
        actor: str,
        expected_payload: Mapping[str, Any] | None,
    ) -> sqlite3.Row | None:
        events = connection.execute(
            "SELECT * FROM events WHERE request_id = ? AND kind = ? ORDER BY sequence",
            (row["request_id"], kind),
        ).fetchall()
        if expected_payload is None:
            if events:
                raise AdvisorConflict(
                    f"advisor {kind} event has no matching current projection"
                )
            return None
        if len(events) != 1:
            raise AdvisorError(f"advisor job must have exactly one {kind} event")
        event = events[0]
        try:
            payload = json.loads(event["payload_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise AdvisorError(f"advisor {kind} event is not valid JSON") from exc
        material = {
            "actor": event["actor"],
            "created_at_utc": event["created_at_utc"],
            "event_id": event["event_id"],
            "kind": event["kind"],
            "payload": payload,
            "previous_digest": event["previous_digest"],
            "request_id": event["request_id"],
        }
        if _sha256_text(_canonical_json(material)) != event["digest"]:
            raise AdvisorConflict(f"advisor {kind} event digest is invalid")
        if (
            event["actor"] != actor
            or event["request_id"] != row["request_id"]
            or event["kind"] != kind
            or payload != dict(expected_payload)
        ):
            raise AdvisorConflict(
                f"advisor {kind} event differs from the current job projection"
            )
        return event

    def _attest_prepared_lineage_event(
        self,
        connection: sqlite3.Connection,
        row: Mapping[str, Any],
    ) -> None:
        try:
            question_raw = str(row["question"]).encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise AdvisorError("advisor question is not valid UTF-8") from exc
        expected = {
            "computer_use_skill_sha256": row["computer_use_skill_sha256"],
            "problem_id": row["problem_id"],
            "query_skill_sha256": row["query_skill_sha256"],
            "question_bytes": len(question_raw),
            "question_sha256": _sha256_bytes(question_raw),
            "run_id": row["run_id"],
            "lineage": _lineage_payload(row),
            "transport": TRANSPORT,
        }
        if (
            row["question_bytes"] != len(question_raw)
            or row["question_sha256"] != expected["question_sha256"]
        ):
            raise AdvisorConflict(
                "advisor question bytes differ from the durable job commitment"
            )
        self._attest_exact_event(
            connection,
            row,
            kind="advisor_question_prepared",
            actor="owner",
            expected_payload=expected,
        )

    def _attest_authorization_event(
        self,
        connection: sqlite3.Connection,
        row: Mapping[str, Any],
    ) -> None:
        authorization_id = row["authorization_id"]
        if authorization_id is None:
            if row["authorized_at_utc"] is not None or row["state"] not in {
                "prepared",
                "abandoned",
            }:
                raise AdvisorConflict(
                    "advisor state or timestamp has no authorization id"
                )
            expected = None
        else:
            if (
                not isinstance(authorization_id, str)
                or AUTHORIZATION_ID_RE.fullmatch(authorization_id) is None
                or not isinstance(row["authorized_at_utc"], str)
                or row["state"] == "prepared"
            ):
                raise AdvisorError("advisor authorization projection is invalid")
            expected = {
                "authorization_id": authorization_id,
                "destination": "chatgpt.com",
                "question_sha256": row["question_sha256"],
                "transport": TRANSPORT,
            }
        self._attest_exact_event(
            connection,
            row,
            kind="advisor_question_authorized",
            actor="owner",
            expected_payload=expected,
        )

    def _attest_dispatch_event(
        self,
        connection: sqlite3.Connection,
        row: Mapping[str, Any],
    ) -> None:
        """Bind the one-click projection to its append-only dispatch event."""

        dispatch_count = row["dispatch_count"]
        if (
            isinstance(dispatch_count, bool)
            or not isinstance(dispatch_count, int)
            or dispatch_count not in {0, 1}
        ):
            raise AdvisorConflict("advisor dispatch_count projection is invalid")
        state = str(row["state"])
        post_dispatch_states = {
            "dispatching",
            "submitted",
            "submission_unknown",
            "completed",
            "needs_user_input",
            "owner_abandoned_outcome_unknown",
            "imported",
            "delivery_unknown",
        }
        if state == "failed_not_submitted":
            expected_dispatch = dispatch_count == 1
        else:
            expected_dispatch = state in post_dispatch_states
        if expected_dispatch != (dispatch_count == 1):
            raise AdvisorConflict(
                "advisor state and dispatch_count have no coherent click boundary"
            )
        expected = None
        if dispatch_count == 1:
            conversation_digest = (
                None
                if row["lineage_kind"] == "none"
                else row["lineage_conversation_url_sha256"]
            )
            expected = {
                "dispatch_count": 1,
                "question_sha256": row["question_sha256"],
                "lineage_kind": row["lineage_kind"],
                "lineage_conversation_url_sha256": conversation_digest,
                "transport": TRANSPORT,
                "ui_mode": UI_MODE,
            }
        self._attest_exact_event(
            connection,
            row,
            kind="advisor_browser_dispatch_started",
            actor="owner",
            expected_payload=expected,
        )

    def _append_event(
        self,
        connection: sqlite3.Connection,
        *,
        request_id: str,
        kind: str,
        actor: str,
        payload: Mapping[str, Any],
    ) -> tuple[int, str]:
        previous_row = connection.execute(
            "SELECT value FROM metadata WHERE key = 'head_digest'"
        ).fetchone()
        previous = str(previous_row["value"] if previous_row else ZERO_DIGEST)
        event_id = f"aevt_{uuid.uuid4().hex}"
        created_at = _utc_now()
        payload_json = _canonical_json(dict(payload))
        material = {
            "actor": actor,
            "created_at_utc": created_at,
            "event_id": event_id,
            "kind": kind,
            "payload": json.loads(payload_json),
            "previous_digest": previous,
            "request_id": request_id,
        }
        digest = _sha256_text(_canonical_json(material))
        cursor = connection.execute(
            """
            INSERT INTO events(
                event_id, request_id, kind, actor, created_at_utc,
                payload_json, previous_digest, digest
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                request_id,
                kind,
                actor,
                created_at,
                payload_json,
                previous,
                digest,
            ),
        )
        connection.execute(
            "UPDATE metadata SET value = ? WHERE key = 'head_digest'", (digest,)
        )
        return int(cursor.lastrowid), digest

    def prepare(
        self,
        *,
        run_id: str,
        problem_id: str,
        question: str,
        query_skill_sha256: str,
        computer_use_skill_sha256: str,
        request_id: str | None = None,
        predecessor_request_id: str | None = None,
        external_source_repo: str | None = None,
        external_request_id: str | None = None,
        external_receipt_sha256: str | None = None,
        external_source_context_sha256: str | None = None,
        external_conversation_url_sha256: str | None = None,
        external_owner_ack: str | None = None,
        external_conversation_url: str | None = None,
    ) -> dict[str, Any]:
        run_id = _validate_run_id(run_id)
        problem_id = _validate_problem_id(problem_id)
        question, question_bytes = _bounded_text(
            question, label="question", maximum=MAX_QUESTION_BYTES
        )
        query_skill_sha256 = _validate_sha256(query_skill_sha256, "query skill digest")
        computer_use_skill_sha256 = _validate_sha256(
            computer_use_skill_sha256, "computer-use skill digest"
        )
        request_id = _validate_request_id(request_id or f"adv_{uuid.uuid4().hex}")
        question_sha = _sha256_text(question)
        now = _utc_now()
        external_values = (
            external_source_repo,
            external_request_id,
            external_receipt_sha256,
            external_source_context_sha256,
            external_conversation_url_sha256,
            external_owner_ack,
            external_conversation_url,
        )
        if predecessor_request_id is not None and any(
            value is not None for value in external_values
        ):
            raise ValueError(
                "local predecessor and owner-asserted external lineage are mutually exclusive"
            )
        if predecessor_request_id is not None:
            predecessor_request_id = _validate_request_id(predecessor_request_id)
            if predecessor_request_id == request_id:
                raise ValueError("advisor request cannot precede itself")
            lineage_kind = "rethlas_predecessor"
        elif any(value is not None for value in external_values):
            if any(value is None for value in external_values):
                raise ValueError(
                    "external lineage requires repo, request, receipt, context, "
                    "acknowledgement, and conversation URL"
                )
            if external_source_repo != EXTERNAL_SOURCE_REPO:
                raise ValueError("external lineage source_repo must be exactly Danus")
            external_request_id = _validate_external_request_id(
                str(external_request_id)
            )
            external_receipt_sha256 = _validate_sha256(
                str(external_receipt_sha256), "external receipt digest"
            )
            external_source_context_sha256 = _validate_sha256(
                str(external_source_context_sha256), "external source context digest"
            )
            external_conversation_url_sha256 = _validate_sha256(
                str(external_conversation_url_sha256),
                "external conversation URL digest",
            )
            if external_owner_ack != EXTERNAL_LINEAGE_ACK:
                raise ValueError(
                    "external lineage requires the exact owner acknowledgement"
                )
            lineage_kind = "owner_asserted_external"
        else:
            lineage_kind = "none"
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM jobs WHERE request_id = ?", (request_id,)
            ).fetchone()
            if existing is not None:
                self._attest_lineage(connection, existing)
                replay_fields = {
                    "computer_use_skill_sha256": computer_use_skill_sha256,
                    "external_owner_ack": external_owner_ack,
                    "external_receipt_sha256": external_receipt_sha256,
                    "external_request_id": external_request_id,
                    "external_source_context_sha256": external_source_context_sha256,
                    "external_source_repo": external_source_repo,
                    "lineage_kind": lineage_kind,
                    "predecessor_request_id": predecessor_request_id,
                    "problem_id": problem_id,
                    "query_skill_sha256": query_skill_sha256,
                    "question": question,
                    "run_id": run_id,
                }
                if any(existing[key] != value for key, value in replay_fields.items()):
                    raise AdvisorConflict(
                        "request_id is already bound to different immutable input"
                    )
                if lineage_kind == "owner_asserted_external":
                    supplied_url_digest = _conversation_url_digest(
                        str(external_conversation_url)
                    )
                    if (
                        supplied_url_digest
                        != existing["lineage_conversation_url_sha256"]
                    ):
                        raise AdvisorConflict(
                            "external conversation URL changed on prepare replay"
                        )
                connection.commit()
                return self.status(request_id)
            predecessor_receipt_sha256: str | None = None
            predecessor_state_at_prepare: str | None = None
            if lineage_kind == "rethlas_predecessor":
                predecessor = self._job(connection, str(predecessor_request_id))
                self._attest_lineage(connection, predecessor)
                if (
                    predecessor["run_id"] != run_id
                    or predecessor["problem_id"] != problem_id
                ):
                    raise AdvisorConflict(
                        "predecessor must belong to the same problem_id and run_id"
                    )
                if predecessor["state"] not in {"completed", "imported"}:
                    raise AdvisorError(
                        "predecessor must be terminal completed or imported"
                    )
                self._verify_terminal_receipt(predecessor)
                predecessor_receipt_sha256 = str(predecessor["receipt_sha256"])
                predecessor_state_at_prepare = str(predecessor["state"])
                lineage_root_request_id = str(predecessor["lineage_root_request_id"])
                lineage_depth = int(predecessor["lineage_depth"]) + 1
                lineage_conversation_url_sha256 = predecessor["conversation_url_sha256"]
                if (
                    not isinstance(lineage_conversation_url_sha256, str)
                    or SHA256_RE.fullmatch(lineage_conversation_url_sha256) is None
                ):
                    raise AdvisorError(
                        "predecessor has no valid ChatGPT conversation binding"
                    )
            elif lineage_kind == "owner_asserted_external":
                lineage_root_request_id = request_id
                lineage_depth = 1
                lineage_conversation_url_sha256 = _conversation_url_digest(
                    str(external_conversation_url)
                )
                if lineage_conversation_url_sha256 != external_conversation_url_sha256:
                    raise AdvisorConflict(
                        "external conversation URL differs from the Danus owner assertion"
                    )
            else:
                lineage_root_request_id = request_id
                lineage_depth = 0
                lineage_conversation_url_sha256 = None
            immutable = {
                "computer_use_skill_sha256": computer_use_skill_sha256,
                "external_owner_ack": external_owner_ack,
                "external_receipt_sha256": external_receipt_sha256,
                "external_request_id": external_request_id,
                "external_source_context_sha256": external_source_context_sha256,
                "external_source_repo": external_source_repo,
                "lineage_conversation_url_sha256": (lineage_conversation_url_sha256),
                "lineage_depth": lineage_depth,
                "lineage_kind": lineage_kind,
                "lineage_root_request_id": lineage_root_request_id,
                "predecessor_receipt_sha256": predecessor_receipt_sha256,
                "predecessor_request_id": predecessor_request_id,
                "predecessor_state_at_prepare": predecessor_state_at_prepare,
                "problem_id": problem_id,
                "query_skill_sha256": query_skill_sha256,
                "question": question,
                "run_id": run_id,
            }
            blocked = connection.execute(
                """
                SELECT request_id FROM jobs
                WHERE question_sha256 = ?
                  AND (state IN ('prepared', 'authorized', 'dispatching',
                                 'submitted', 'submission_unknown')
                       OR outcome_unknown_abandoned = 1)
                LIMIT 1
                """,
                (question_sha,),
            ).fetchone()
            if blocked is not None:
                raise AdvisorConflict(
                    "this exact question is globally blocked by an unresolved or "
                    "owner-abandoned unknown submission"
                )
            connection.execute(
                """
                INSERT INTO jobs(
                    request_id, run_id, problem_id, owner_uid, state, question,
                    question_sha256, question_bytes, query_skill_sha256,
                    computer_use_skill_sha256, lineage_kind,
                    predecessor_request_id, predecessor_receipt_sha256,
                    predecessor_state_at_prepare, lineage_root_request_id,
                    lineage_depth, lineage_conversation_url_sha256,
                    external_source_repo, external_request_id,
                    external_receipt_sha256, external_source_context_sha256,
                    external_owner_ack, created_at_utc, updated_at_utc
                ) VALUES (?, ?, ?, ?, 'prepared', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                          ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    request_id,
                    run_id,
                    problem_id,
                    self._owner_uid(),
                    question,
                    question_sha,
                    question_bytes,
                    query_skill_sha256,
                    computer_use_skill_sha256,
                    lineage_kind,
                    predecessor_request_id,
                    predecessor_receipt_sha256,
                    predecessor_state_at_prepare,
                    lineage_root_request_id,
                    lineage_depth,
                    lineage_conversation_url_sha256,
                    external_source_repo,
                    external_request_id,
                    external_receipt_sha256,
                    external_source_context_sha256,
                    external_owner_ack,
                    now,
                    now,
                ),
            )
            self._append_event(
                connection,
                request_id=request_id,
                kind="advisor_question_prepared",
                actor="owner",
                payload={
                    "computer_use_skill_sha256": computer_use_skill_sha256,
                    "problem_id": problem_id,
                    "query_skill_sha256": query_skill_sha256,
                    "question_bytes": question_bytes,
                    "question_sha256": question_sha,
                    "run_id": run_id,
                    "lineage": _lineage_payload(immutable),
                    "transport": TRANSPORT,
                },
            )
            connection.commit()
        return self.status(request_id)

    def authorize(
        self,
        request_id: str,
        *,
        authorization_id: str,
        question_sha256: str,
    ) -> dict[str, Any]:
        if (
            not isinstance(authorization_id, str)
            or AUTHORIZATION_ID_RE.fullmatch(authorization_id) is None
        ):
            raise ValueError("authorization_id has an unsafe shape")
        question_sha256 = _validate_sha256(question_sha256, "question digest")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._job(connection, request_id)
            self._attest_lineage(connection, row)
            if row["question_sha256"] != question_sha256:
                raise AdvisorConflict("authorization does not bind the exact question")
            if row["state"] == "authorized":
                if row["authorization_id"] != authorization_id:
                    raise AdvisorConflict("request already has another authorization")
                connection.commit()
                return self.status(request_id)
            if row["state"] != "prepared":
                raise AdvisorError(f"cannot authorize from state {row['state']}")
            event_sequence, _ = self._append_event(
                connection,
                request_id=request_id,
                kind="advisor_question_authorized",
                actor="owner",
                payload={
                    "authorization_id": authorization_id,
                    "destination": "chatgpt.com",
                    "question_sha256": question_sha256,
                    "transport": TRANSPORT,
                },
            )
            authorized_at = str(
                connection.execute(
                    "SELECT created_at_utc FROM events WHERE sequence = ?",
                    (event_sequence,),
                ).fetchone()["created_at_utc"]
            )
            connection.execute(
                """
                UPDATE jobs SET state = 'authorized', authorization_id = ?,
                    authorized_at_utc = ?, updated_at_utc = ?
                WHERE request_id = ?
                """,
                (authorization_id, authorized_at, authorized_at, request_id),
            )
            connection.commit()
        return self.status(request_id)

    def begin_dispatch(
        self,
        request_id: str,
        *,
        conversation_url: str | None = None,
    ) -> dict[str, Any]:
        """Cross the durable pre-click boundary exactly once."""

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._job(connection, request_id)
            self._attest_lineage(connection, row)
            if row["state"] != "authorized" or int(row["dispatch_count"]) != 0:
                raise AdvisorError(
                    "browser submission may begin exactly once from authorized; "
                    "never retry a dispatching or unknown submission"
                )
            if row["lineage_kind"] == "none":
                if conversation_url is not None:
                    raise AdvisorError(
                        "new-chat dispatch accepts no continuation conversation URL"
                    )
                conversation_digest = None
            else:
                if conversation_url is None:
                    raise AdvisorError(
                        "continuation dispatch requires the exact conversation URL"
                    )
                conversation_digest = _conversation_url_digest(conversation_url)
                if conversation_digest != row["lineage_conversation_url_sha256"]:
                    raise AdvisorConflict(
                        "continuation URL differs from its prepared lineage binding"
                    )
            conflicting = connection.execute(
                """
                SELECT request_id FROM jobs
                WHERE request_id != ? AND question_sha256 = ?
                  AND (state IN ('prepared', 'authorized', 'dispatching',
                                 'submitted', 'submission_unknown')
                       OR outcome_unknown_abandoned = 1)
                LIMIT 1
                """,
                (request_id, row["question_sha256"]),
            ).fetchone()
            if conflicting is not None:
                raise AdvisorConflict(
                    "another advisor job now owns this exact question; browser "
                    "dispatch is forbidden"
                )
            self._append_event(
                connection,
                request_id=request_id,
                kind="advisor_browser_dispatch_started",
                actor="owner",
                payload={
                    "dispatch_count": 1,
                    "question_sha256": row["question_sha256"],
                    "lineage_kind": row["lineage_kind"],
                    "lineage_conversation_url_sha256": conversation_digest,
                    "transport": TRANSPORT,
                    "ui_mode": UI_MODE,
                },
            )
            connection.execute(
                """
                UPDATE jobs SET state = 'dispatching', dispatch_count = 1,
                    conversation_url_sha256 = ?,
                    updated_at_utc = ? WHERE request_id = ?
                """,
                (conversation_digest, _utc_now(), request_id),
            )
            connection.commit()
        result = self.status(request_id)
        result["question"] = row["question"]
        # These capabilities exist only in the direct return from the one
        # transaction that changed authorized -> dispatching. A later status
        # read never recreates click permission, and a replay is rejected.
        result["transitioned"] = True
        result["click_authorized"] = True
        return result

    def mark_submitted(
        self,
        request_id: str,
        *,
        conversation_url: str,
        observed_question: str,
        ui_mode: str,
        recover_unknown: bool = False,
    ) -> dict[str, Any]:
        observed_question, observed_question_bytes = _bounded_text(
            observed_question,
            label="observed browser question",
            maximum=MAX_QUESTION_BYTES,
        )
        observed_question_sha256 = _sha256_text(observed_question)
        if ui_mode != UI_MODE:
            raise AdvisorError("visible ChatGPT composer mode must be exactly Pro")
        conversation_digest = _conversation_url_digest(conversation_url)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._job(connection, request_id)
            self._attest_lineage(connection, row)
            if observed_question_sha256 != row["question_sha256"]:
                raise AdvisorConflict(
                    "visible browser question differs from authorization"
                )
            if (
                row["conversation_url_sha256"] is not None
                and row["conversation_url_sha256"] != conversation_digest
            ):
                raise AdvisorConflict(
                    "submission can only be recorded in its original conversation binding"
                )
            expected_state = "submission_unknown" if recover_unknown else "dispatching"
            if row["state"] == "submitted":
                if row["conversation_url_sha256"] != conversation_digest:
                    raise AdvisorConflict(
                        "request is already bound to another conversation"
                    )
                connection.commit()
                return self.status(request_id)
            if row["state"] != expected_state:
                raise AdvisorError(
                    f"cannot mark submitted from state {row['state']}; expected {expected_state}"
                )
            now = _utc_now()
            self._append_event(
                connection,
                request_id=request_id,
                kind=(
                    "advisor_existing_submission_reconciled"
                    if recover_unknown
                    else "advisor_question_submitted"
                ),
                actor="chrome",
                payload={
                    "conversation_url_sha256": conversation_digest,
                    "observed_question_bytes": observed_question_bytes,
                    "question_sha256": observed_question_sha256,
                    "ui_mode": ui_mode,
                },
            )
            connection.execute(
                """
                UPDATE jobs SET state = 'submitted', submitted_at_utc = ?,
                    conversation_url_sha256 = ?, updated_at_utc = ?
                WHERE request_id = ?
                """,
                (now, conversation_digest, now, request_id),
            )
            connection.commit()
        return self.status(request_id)

    def mark_submission_unknown(
        self,
        request_id: str,
        *,
        reason: str,
        conversation_url: str | None = None,
    ) -> dict[str, Any]:
        reason = _redacted_reason(reason)
        conversation_digest = (
            _conversation_url_digest(conversation_url)
            if conversation_url is not None
            else None
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._job(connection, request_id)
            self._attest_lineage(connection, row)
            if row["state"] == "submission_unknown":
                existing_digest = row["conversation_url_sha256"]
                if (
                    existing_digest is not None
                    and conversation_digest is not None
                    and existing_digest != conversation_digest
                ):
                    raise AdvisorConflict(
                        "unknown submission is bound to another conversation"
                    )
                if existing_digest is None and conversation_digest is not None:
                    self._append_event(
                        connection,
                        request_id=request_id,
                        kind="advisor_unknown_conversation_observed",
                        actor="chrome",
                        payload={
                            "conversation_url_sha256": conversation_digest,
                        },
                    )
                    connection.execute(
                        "UPDATE jobs SET conversation_url_sha256 = ?, "
                        "updated_at_utc = ? WHERE request_id = ?",
                        (conversation_digest, _utc_now(), request_id),
                    )
                connection.commit()
                return self.status(request_id)
            if row["state"] not in {"dispatching", "submitted"}:
                raise AdvisorError(
                    f"submission cannot become unknown from state {row['state']}"
                )
            existing_digest = row["conversation_url_sha256"]
            if (
                existing_digest is not None
                and conversation_digest is not None
                and existing_digest != conversation_digest
            ):
                raise AdvisorConflict(
                    "unknown submission must retain its original conversation"
                )
            retained_digest = existing_digest or conversation_digest
            self._append_event(
                connection,
                request_id=request_id,
                kind="advisor_submission_outcome_unknown",
                actor="advisor_bridge",
                payload={
                    "conversation_url_sha256": retained_digest,
                    "reason": reason,
                },
            )
            connection.execute(
                "UPDATE jobs SET state = 'submission_unknown', "
                "conversation_url_sha256 = ?, updated_at_utc = ? "
                "WHERE request_id = ?",
                (retained_digest, _utc_now(), request_id),
            )
            connection.commit()
        return self.status(request_id)

    def failed_not_submitted(
        self,
        request_id: str,
        *,
        reason: str,
        send_not_clicked_confirmed: bool,
    ) -> dict[str, Any]:
        """Record an authoritative terminal pre-Send failure.

        This outcome is intentionally narrower than a generic browser error. It
        is legal only after the owner workflow attempted the pre-Send boundary:
        either the durable CAS is still ``authorized`` (no dispatch permission
        was committed) or it reached ``dispatching`` while the browser operator
        positively confirms that Send was never clicked. Any crash, timeout,
        disconnect, or observation compatible with a click must be recorded as
        ``submission_unknown`` instead.
        """

        if send_not_clicked_confirmed is not True:
            raise AdvisorError(
                "failed_not_submitted requires positive confirmation that Send "
                "was never clicked; otherwise use submission-unknown"
            )
        reason = _redacted_reason(reason)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._job(connection, request_id)
            self._attest_lineage(connection, row)
            if row["state"] == "failed_not_submitted":
                if row["terminal_reason"] != reason:
                    raise AdvisorConflict(
                        "failed_not_submitted replay has a different reason"
                    )
                connection.commit()
                return self.status(request_id)
            if row["state"] not in {"authorized", "dispatching"}:
                raise AdvisorError(
                    "failed_not_submitted is valid only from authorized or "
                    "dispatching before any possible Send click"
                )
            receipt = _failed_not_submitted_receipt_payload(
                row, reason=reason, prior_state=str(row["state"])
            )
            raw = (_canonical_json(receipt) + "\n").encode("utf-8")
            receipt_sha = _sha256_bytes(raw)
            self._discard_uncommitted_terminal_receipt(request_id)
            self._append_event(
                connection,
                request_id=request_id,
                kind="advisor_failed_not_submitted",
                actor="owner",
                payload={
                    "browser_submission_possible": False,
                    "prior_state": row["state"],
                    "reason": reason,
                    "receipt_sha256": receipt_sha,
                    "send_clicked": False,
                },
            )
            connection.execute(
                "UPDATE jobs SET state = 'failed_not_submitted', "
                "receipt_sha256 = ?, receipt_published = 0, "
                "terminal_reason = ?, updated_at_utc = ? "
                "WHERE request_id = ?",
                (receipt_sha, reason, _utc_now(), request_id),
            )
            connection.commit()
        return self.status(request_id)

    def complete(
        self,
        request_id: str,
        *,
        answer: str,
        answer_snapshot_a_sha256: str,
        answer_snapshot_b_sha256: str,
        ui_mode: str,
        response_actions_present: bool,
        composer_available: bool,
        working_indicators_absent: bool,
    ) -> dict[str, Any]:
        answer, answer_bytes = _bounded_text(
            answer, label="answer", maximum=MAX_ANSWER_BYTES
        )
        answer_sha = _sha256_text(answer)
        first = _validate_sha256(answer_snapshot_a_sha256, "first answer snapshot")
        second = _validate_sha256(answer_snapshot_b_sha256, "second answer snapshot")
        if first != second or first != answer_sha:
            raise AdvisorError(
                "two stable answer snapshots must equal the exact answer"
            )
        if ui_mode != UI_MODE:
            raise AdvisorError("visible ChatGPT composer mode must be exactly Pro")
        if not (
            response_actions_present
            and composer_available
            and working_indicators_absent
        ):
            raise AdvisorError("ChatGPT Pro completion signals are incomplete")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._job(connection, request_id)
            self._attest_lineage(connection, row)
            if row["state"] == "completed":
                if row["answer_sha256"] != answer_sha:
                    raise AdvisorConflict(
                        "completed request has different answer bytes"
                    )
                self._verify_completion_receipt(row)
                connection.commit()
                return self.status(request_id)
            if row["state"] != "submitted":
                raise AdvisorError(f"cannot complete from state {row['state']}")
            now = _utc_now()
            receipt = _completion_receipt_payload(
                row,
                answer_bytes=answer_bytes,
                answer_sha256=answer_sha,
            )
            raw = (_canonical_json(receipt) + "\n").encode("utf-8")
            if len(raw) > MAX_RECEIPT_BYTES:
                raise AdvisorError("advisor receipt exceeds its durable size limit")
            receipt_sha = _sha256_bytes(raw)
            self._discard_uncommitted_terminal_receipt(request_id)
            self._append_event(
                connection,
                request_id=request_id,
                kind="advisor_response_completed",
                actor="chrome",
                payload={
                    "answer_bytes": answer_bytes,
                    "answer_sha256": answer_sha,
                    "receipt_sha256": receipt_sha,
                    "stable_answer_sha256": answer_sha,
                    "ui_mode": UI_MODE,
                },
            )
            connection.execute(
                """
                UPDATE jobs SET state = 'completed', answer = NULL, answer_sha256 = ?,
                    answer_bytes = ?, stable_answer_sha256 = ?, completed_at_utc = ?,
                    receipt_sha256 = ?, receipt_published = 0,
                    updated_at_utc = ? WHERE request_id = ?
                """,
                (
                    answer_sha,
                    answer_bytes,
                    answer_sha,
                    now,
                    receipt_sha,
                    now,
                    request_id,
                ),
            )
            connection.commit()
        return self.status(request_id)

    def needs_user_input(
        self, request_id: str, *, clarification: str
    ) -> dict[str, Any]:
        clarification, clarification_bytes = _bounded_text(
            clarification,
            label="clarification",
            maximum=MAX_CLARIFICATION_BYTES,
        )
        clarification_sha = _sha256_text(clarification)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._job(connection, request_id)
            self._attest_lineage(connection, row)
            if row["state"] == "needs_user_input":
                if row["clarification_sha256"] != clarification_sha:
                    raise AdvisorConflict("different clarification already recorded")
                connection.commit()
                result = self.status(request_id)
                result["clarification"] = clarification
                result["clarification_ephemeral"] = True
                return result
            if row["state"] != "submitted":
                raise AdvisorError(
                    f"clarification cannot be recorded from state {row['state']}"
                )
            receipt = _needs_user_input_receipt_payload(
                row,
                clarification_bytes=clarification_bytes,
                clarification_sha256=clarification_sha,
            )
            raw = (_canonical_json(receipt) + "\n").encode("utf-8")
            receipt_sha = _sha256_bytes(raw)
            self._discard_uncommitted_terminal_receipt(request_id)
            self._append_event(
                connection,
                request_id=request_id,
                kind="advisor_needs_user_input",
                actor="chrome",
                payload={
                    "clarification_bytes": clarification_bytes,
                    "clarification_sha256": clarification_sha,
                    "automatic_followup_allowed": False,
                    "receipt_sha256": receipt_sha,
                },
            )
            connection.execute(
                """
                UPDATE jobs SET state = 'needs_user_input',
                    clarification = NULL, clarification_bytes = ?,
                    clarification_sha256 = ?,
                    receipt_sha256 = ?, receipt_published = 0, updated_at_utc = ?
                WHERE request_id = ?
                """,
                (
                    clarification_bytes,
                    clarification_sha,
                    receipt_sha,
                    _utc_now(),
                    request_id,
                ),
            )
            connection.commit()
        result = self.status(request_id)
        result["clarification"] = clarification
        result["clarification_ephemeral"] = True
        return result

    def _verify_completion_receipt(self, row: Mapping[str, Any]) -> bytes:
        request_id = _validate_request_id(str(row["request_id"]))
        receipt_path = self.receipts_root / f"{request_id}.json"
        if not self._receipt_exists(receipt_path):
            raise AdvisorError("completed advisor commitment receipt is missing")
        raw = self._existing_receipt_bytes(receipt_path)
        expected_digest = row["receipt_sha256"]
        if (
            not isinstance(expected_digest, str)
            or SHA256_RE.fullmatch(expected_digest) is None
            or _sha256_bytes(raw) != expected_digest
        ):
            raise AdvisorConflict(
                "completed advisor commitment digest differs from the durable ledger"
            )
        try:
            decoded = raw.decode("utf-8", errors="strict")
            receipt = json.loads(
                decoded,
                object_pairs_hook=_strict_json_object,
                parse_constant=_reject_json_constant,
            )
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise AdvisorError(
                "completed advisor commitment is not strict JSON"
            ) from exc
        if not isinstance(receipt, dict):
            raise AdvisorError("completed advisor commitment must be a JSON object")
        try:
            canonical = (_canonical_json(receipt) + "\n").encode("utf-8")
        except (TypeError, UnicodeError, ValueError) as exc:
            raise AdvisorError(
                "completed advisor commitment is not canonical JSON"
            ) from exc
        if raw != canonical:
            raise AdvisorConflict("completed advisor commitment is not canonical JSON")
        answer_bytes = row["answer_bytes"]
        answer_sha256 = row["answer_sha256"]
        if (
            row["answer"] is not None
            or not isinstance(answer_bytes, int)
            or isinstance(answer_bytes, bool)
            or not isinstance(answer_sha256, str)
            or SHA256_RE.fullmatch(answer_sha256) is None
            or not isinstance(row["question"], str)
            or not isinstance(row["question_bytes"], int)
            or isinstance(row["question_bytes"], bool)
            or not isinstance(row["question_sha256"], str)
            or SHA256_RE.fullmatch(row["question_sha256"]) is None
        ):
            raise AdvisorError("completed advisor commitment fields are incomplete")
        try:
            question_raw = row["question"].encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise AdvisorError("completed advisor ledger text is not UTF-8") from exc
        if (
            answer_bytes <= 0
            or len(question_raw) != row["question_bytes"]
            or _sha256_bytes(question_raw) != row["question_sha256"]
        ):
            raise AdvisorConflict(
                "completed advisor commitment fields are inconsistent"
            )
        for field in (
            "computer_use_skill_sha256",
            "conversation_url_sha256",
            "query_skill_sha256",
        ):
            value = row[field]
            if not isinstance(value, str) or SHA256_RE.fullmatch(value) is None:
                raise AdvisorError(f"completed advisor ledger has invalid {field}")
        expected = _completion_receipt_payload(
            row,
            answer_bytes=answer_bytes,
            answer_sha256=answer_sha256,
        )
        if receipt != expected:
            raise AdvisorConflict(
                "completed advisor commitment contents differ from the durable ledger"
            )
        return raw

    def _verify_report_receipt(self, row: Mapping[str, Any]) -> bytes:
        request_id = _validate_request_id(str(row["request_id"]))
        receipt_path = self.receipts_root / f"{request_id}.report.json"
        if not self._receipt_exists(receipt_path):
            raise AdvisorError("materialized advisor report receipt is missing")
        raw = self._existing_receipt_bytes(receipt_path)
        expected_digest = row["report_receipt_sha256"]
        if (
            not isinstance(expected_digest, str)
            or SHA256_RE.fullmatch(expected_digest) is None
            or _sha256_bytes(raw) != expected_digest
        ):
            raise AdvisorConflict(
                "materialized advisor report digest differs from the durable ledger"
            )
        try:
            decoded = raw.decode("utf-8", errors="strict")
            receipt = json.loads(
                decoded,
                object_pairs_hook=_strict_json_object,
                parse_constant=_reject_json_constant,
            )
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise AdvisorError(
                "materialized advisor report is not strict JSON"
            ) from exc
        if not isinstance(receipt, dict):
            raise AdvisorError("materialized advisor report must be a JSON object")
        try:
            canonical = (_canonical_json(receipt) + "\n").encode("utf-8")
        except (TypeError, UnicodeError, ValueError) as exc:
            raise AdvisorError(
                "materialized advisor report is not canonical JSON"
            ) from exc
        if raw != canonical:
            raise AdvisorConflict("materialized advisor report is not canonical JSON")
        answer = receipt.get("answer")
        if not isinstance(answer, str):
            raise AdvisorError("materialized advisor report answer is missing")
        try:
            answer_raw = answer.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise AdvisorError("materialized advisor answer is not UTF-8") from exc
        if (
            len(answer_raw) != row["answer_bytes"]
            or _sha256_bytes(answer_raw) != row["answer_sha256"]
        ):
            raise AdvisorConflict(
                "materialized advisor answer differs from its completion commitment"
            )
        expected = _report_receipt_payload(
            row,
            answer=answer,
            answer_bytes=int(row["answer_bytes"]),
            answer_sha256=str(row["answer_sha256"]),
        )
        if receipt != expected:
            raise AdvisorConflict(
                "materialized advisor report contents differ from the durable ledger"
            )
        return raw

    def _verify_terminal_receipt(self, row: Mapping[str, Any]) -> None:
        """Fail closed unless every durable terminal receipt matches its job."""

        state = str(row["state"])
        receipt_states = {
            "completed",
            "delivery_unknown",
            "imported",
            "failed_not_submitted",
            "needs_user_input",
            "owner_abandoned_outcome_unknown",
        }
        published = row["receipt_published"]
        if (
            isinstance(published, bool)
            or not isinstance(published, int)
            or published not in {0, 1}
        ):
            raise AdvisorError("advisor receipt publication projection is invalid")
        if state in receipt_states and published != 1:
            raise AdvisorError(f"{state} terminal receipt publication is incomplete")
        if state not in receipt_states and published != 0:
            raise AdvisorConflict(
                "non-receipt advisor state unexpectedly claims publication"
            )
        if state in {"completed", "delivery_unknown", "imported"}:
            self._verify_completion_receipt(row)
            if state in {"delivery_unknown", "imported"}:
                self._verify_report_receipt(row)
            elif row["report_receipt_sha256"] is not None:
                # Recoverable crash gap after report materialization but before
                # the local delivery ambiguity boundary was committed.
                self._verify_report_receipt(row)
            return
        if state == "abandoned":
            if row["receipt_sha256"] is not None:
                raise AdvisorConflict(
                    "non-submitted abandonment unexpectedly has a receipt digest"
                )
            return

        reason = row["terminal_reason"]
        if state == "failed_not_submitted":
            dispatch_count = row["dispatch_count"]
            if (
                not isinstance(dispatch_count, int)
                or isinstance(dispatch_count, bool)
                or dispatch_count not in {0, 1}
                or not isinstance(reason, str)
            ):
                raise AdvisorError(
                    "failed_not_submitted terminal ledger fields are invalid"
                )
            expected = _failed_not_submitted_receipt_payload(
                row,
                reason=reason,
                prior_state="dispatching" if dispatch_count == 1 else "authorized",
            )
        elif state == "needs_user_input":
            clarification_bytes = row["clarification_bytes"]
            clarification_sha = row["clarification_sha256"]
            if (
                row["clarification"] is not None
                or not isinstance(clarification_bytes, int)
                or isinstance(clarification_bytes, bool)
                or clarification_bytes <= 0
                or not isinstance(clarification_sha, str)
                or SHA256_RE.fullmatch(clarification_sha) is None
            ):
                raise AdvisorError(
                    "needs_user_input terminal ledger fields are invalid"
                )
            expected = _needs_user_input_receipt_payload(
                row,
                clarification_bytes=clarification_bytes,
                clarification_sha256=clarification_sha,
            )
        elif state == "owner_abandoned_outcome_unknown":
            if row["outcome_unknown_abandoned"] != 1 or not isinstance(reason, str):
                raise AdvisorError("unknown-abandon terminal ledger fields are invalid")
            expected = _abandoned_unknown_receipt_payload(row, reason=reason)
        else:
            return

        request_id = _validate_request_id(str(row["request_id"]))
        receipt_path = self.receipts_root / f"{request_id}.json"
        if not self._receipt_exists(receipt_path):
            raise AdvisorError(f"{state} terminal receipt is missing")
        raw = self._existing_receipt_bytes(receipt_path)
        expected_digest = row["receipt_sha256"]
        if (
            not isinstance(expected_digest, str)
            or SHA256_RE.fullmatch(expected_digest) is None
            or _sha256_bytes(raw) != expected_digest
        ):
            raise AdvisorConflict(
                f"{state} terminal receipt digest differs from the durable ledger"
            )
        try:
            decoded = raw.decode("utf-8", errors="strict")
            receipt = json.loads(
                decoded,
                object_pairs_hook=_strict_json_object,
                parse_constant=_reject_json_constant,
            )
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise AdvisorError(f"{state} terminal receipt is not strict JSON") from exc
        if not isinstance(receipt, dict):
            raise AdvisorError(f"{state} terminal receipt must be a JSON object")
        try:
            canonical = (_canonical_json(receipt) + "\n").encode("utf-8")
        except (TypeError, UnicodeError, ValueError) as exc:
            raise AdvisorError(
                f"{state} terminal receipt is not canonical JSON"
            ) from exc
        if raw != canonical:
            raise AdvisorConflict(f"{state} terminal receipt is not canonical JSON")
        if receipt != expected:
            raise AdvisorConflict(
                f"{state} terminal receipt contents differ from the durable ledger"
            )

    def _materialize_report(self, request_id: str, *, answer: str) -> None:
        answer, answer_bytes = _bounded_text(
            answer, label="answer", maximum=MAX_ANSWER_BYTES
        )
        answer_sha = _sha256_text(answer)
        self._publish_pending_terminal_receipt(request_id)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._job(connection, request_id)
            self._attest_lineage(connection, row)
            if row["state"] not in {"completed", "delivery_unknown", "imported"}:
                raise AdvisorError(
                    f"advisor report cannot be materialized from state {row['state']}"
                )
            self._verify_completion_receipt(row)
            if (
                row["answer_sha256"] != answer_sha
                or row["answer_bytes"] != answer_bytes
            ):
                raise AdvisorConflict(
                    "imported advisor response differs from its completion commitment"
                )
            if row["state"] != "completed":
                self._verify_report_receipt(row)
                connection.commit()
                return
            receipt = _report_receipt_payload(
                row,
                answer=answer,
                answer_bytes=answer_bytes,
                answer_sha256=answer_sha,
            )
            raw = (_canonical_json(receipt) + "\n").encode("utf-8")
            if len(raw) > MAX_RECEIPT_BYTES:
                raise AdvisorError(
                    "advisor report receipt exceeds its durable size limit"
                )
            report_sha = _sha256_bytes(raw)
            report_path = self.receipts_root / f"{request_id}.report.json"
            if self._receipt_exists(report_path):
                if self._existing_receipt_bytes(report_path) != raw:
                    raise AdvisorConflict(
                        "advisor report receipt already exists with other bytes"
                    )
            else:
                self._atomic_write(report_path, raw)
            existing_digest = row["report_receipt_sha256"]
            if existing_digest is not None and existing_digest != report_sha:
                raise AdvisorConflict(
                    "advisor report receipt digest changed after materialization"
                )
            if existing_digest is None:
                self._append_event(
                    connection,
                    request_id=request_id,
                    kind="advisor_report_materialized_for_owner_import",
                    actor="owner",
                    payload={
                        "answer_bytes": answer_bytes,
                        "answer_sha256": answer_sha,
                        "report_receipt_sha256": report_sha,
                    },
                )
                connection.execute(
                    "UPDATE jobs SET report_receipt_sha256 = ?, updated_at_utc = ? "
                    "WHERE request_id = ?",
                    (report_sha, _utc_now(), request_id),
                )
            connection.commit()

    def _prepare_delivery(
        self, request_id: str, *, mode: str, retry_unknown: bool
    ) -> tuple[dict[str, Any], str]:
        if mode != "steer":
            raise ValueError(
                "advisor delivery mode must be steer; queue and interrupt are forbidden"
            )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._job(connection, request_id)
            self._attest_lineage(connection, row)
            rejected = connection.execute(
                "SELECT 1 FROM events WHERE request_id = ? "
                "AND kind = 'advisor_report_delivery_rejected' LIMIT 1",
                (request_id,),
            ).fetchone()
            if rejected is not None:
                raise AdvisorError(
                    "advisor report delivery was terminally rejected; later steer is forbidden"
                )
            expected = "delivery_unknown" if retry_unknown else "completed"
            if row["state"] in {"completed", "delivery_unknown", "imported"}:
                self._verify_completion_receipt(row)
                self._verify_report_receipt(row)
            if row["state"] == "imported":
                connection.commit()
                return dict(row), str(row["delivery_client_message_id"])
            if row["state"] != expected:
                raise AdvisorError(
                    f"advisor report cannot be delivered from state {row['state']}"
                )
            if retry_unknown and row["delivery_mode"] != mode:
                raise AdvisorConflict(
                    "delivery retry must preserve the exact original local mode"
                )
            next_count = int(row["delivery_attempt_count"]) + 1
            client_id = str(
                row["delivery_client_message_id"]
                or f"advisor:{request_id}:{str(row['report_receipt_sha256'])[:16]}"
            )
            self._append_event(
                connection,
                request_id=request_id,
                kind=(
                    "advisor_delivery_retry_authorized"
                    if retry_unknown
                    else "advisor_delivery_started"
                ),
                actor="owner",
                payload={
                    "client_message_id": client_id,
                    "delivery_attempt_count": next_count,
                    "mode": mode,
                    "receipt_sha256": row["report_receipt_sha256"],
                },
            )
            # Persist the ambiguity boundary before touching the separate
            # hot-join database.  A crash after this commit never causes an
            # automatic second enqueue.
            connection.execute(
                """
                UPDATE jobs SET state = 'delivery_unknown',
                    delivery_client_message_id = ?, delivery_mode = ?,
                    delivery_attempt_count = ?, updated_at_utc = ?
                WHERE request_id = ?
                """,
                (client_id, mode, next_count, _utc_now(), request_id),
            )
            connection.commit()
        return dict(row), client_id

    def import_report(
        self,
        request_id: str,
        *,
        hotjoin_db: Path | str,
        mode: str,
        retry_unknown: bool = False,
        answer: str | None = None,
    ) -> dict[str, Any]:
        if mode != "steer":
            raise ValueError(
                "advisor delivery mode must be steer; queue and interrupt are forbidden"
            )
        if retry_unknown:
            if answer is not None:
                raise AdvisorError(
                    "retry-delivery reuses the materialized report and accepts no answer"
                )
        else:
            current_status = self.status(request_id)
            state = current_status["state"]
            if (
                current_status["terminal_reason"] is not None
                and current_status["report_receipt_sha256"] is not None
            ):
                raise AdvisorError(
                    "advisor report delivery was terminally rejected; later steer is forbidden"
                )
            if answer is not None and state in {
                "completed",
                "delivery_unknown",
                "imported",
            }:
                self._materialize_report(request_id, answer=answer)
            elif state == "completed":
                raise AdvisorError(
                    "initial advisor import requires the exact committed answer"
                )
            elif answer is not None:
                raise AdvisorError(
                    "answer bytes are accepted only for an existing report import"
                )
        row, client_id = self._prepare_delivery(
            request_id, mode=mode, retry_unknown=retry_unknown
        )
        if row.get("state") == "imported":
            return self.status(request_id)
        self._verify_completion_receipt(row)
        self._verify_report_receipt(row)
        # Import lazily so merely preparing advisor state has no access to the
        # app-server transport or verifier surface.
        try:
            from .hotjoin_adapter import (  # type: ignore[import-not-found]  # noqa: PLC0415
                AdvisorDeliveryRejected,
                ConversationLedger,
                HotJoinError,
                _acquire_database_lifecycle_guard,
                _release_database_lifecycle_guard,
            )
        except ImportError:  # direct ``python agents/advisor_bridge.py`` execution
            from hotjoin_adapter import (  # type: ignore[no-redef]  # noqa: PLC0415
                AdvisorDeliveryRejected,
                ConversationLedger,
                HotJoinError,
                _acquire_database_lifecycle_guard,
                _release_database_lifecycle_guard,
            )

        hotjoin_guard = None
        try:
            hotjoin_guard = _acquire_database_lifecycle_guard(
                hotjoin_db, exclusive=False
            )
            hotjoin = ConversationLedger(hotjoin_db)
            accepted = hotjoin.enqueue_advisor_notice(
                str(row["run_id"]),
                problem_id=str(row["problem_id"]),
                receipt_id=request_id,
                receipt_sha256=str(row["report_receipt_sha256"]),
                authorization_id=str(row["authorization_id"]),
                mode=mode,
                client_message_id=client_id,
            )
        except AdvisorDeliveryRejected as exc:
            reason = _redacted_reason(str(exc))
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                current = self._job(connection, request_id)
                self._attest_lineage(connection, current)
                already_rejected = connection.execute(
                    "SELECT 1 FROM events WHERE request_id = ? "
                    "AND kind = 'advisor_report_delivery_rejected' LIMIT 1",
                    (request_id,),
                ).fetchone()
                if already_rejected is None:
                    if current["state"] != "delivery_unknown":
                        raise AdvisorError(
                            "advisor delivery state changed during rejection"
                        ) from exc
                    self._append_event(
                        connection,
                        request_id=request_id,
                        kind="advisor_report_delivery_rejected",
                        actor="advisor_bridge",
                        payload={
                            "client_message_id": client_id,
                            "mode": mode,
                            "reason": reason,
                            "receipt_sha256": row["report_receipt_sha256"],
                        },
                    )
                    connection.execute(
                        """
                        UPDATE jobs SET state = 'completed', terminal_reason = ?,
                            updated_at_utc = ? WHERE request_id = ?
                        """,
                        (reason, _utc_now(), request_id),
                    )
                connection.commit()
            raise AdvisorError(
                "advisor report delivery was terminally rejected; later steer is forbidden"
            ) from exc
        except HotJoinError as exc:
            raise AdvisorError(
                f"hot-join delivery is unavailable: {_redacted_reason(str(exc))}"
            ) from exc
        finally:
            _release_database_lifecycle_guard(hotjoin_guard)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = self._job(connection, request_id)
            self._attest_lineage(connection, current)
            if current["state"] == "imported":
                connection.commit()
                return self.status(request_id)
            if current["state"] != "delivery_unknown":
                raise AdvisorError("advisor delivery state changed during import")
            self._append_event(
                connection,
                request_id=request_id,
                kind="advisor_report_imported",
                actor="advisor_bridge",
                payload={
                    "accepted_sequence": accepted["accepted_sequence"],
                    "client_message_id": client_id,
                    "hotjoin_message_id": accepted["message_id"],
                    "idempotent_replay": accepted["idempotent_replay"],
                    "mode": mode,
                    "receipt_sha256": row["report_receipt_sha256"],
                    "source_kind": SOURCE_KIND,
                    "expected_thread_id": accepted["expected_thread_id"],
                    "expected_turn_id": accepted["expected_turn_id"],
                },
            )
            connection.execute(
                "UPDATE jobs SET state = 'imported', updated_at_utc = ? "
                "WHERE request_id = ?",
                (_utc_now(), request_id),
            )
            connection.commit()
        return self.status(request_id)

    def abandon(
        self,
        request_id: str,
        *,
        reason: str,
        outcome_unknown_ack: str | None = None,
        question_sha256: str | None = None,
    ) -> dict[str, Any]:
        reason = _redacted_reason(reason)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._job(connection, request_id)
            self._attest_lineage(connection, row)
            if row["state"] in {"abandoned", "owner_abandoned_outcome_unknown"}:
                if row["terminal_reason"] != reason:
                    raise AdvisorConflict("abandon replay has a different reason")
                if row["state"] == "owner_abandoned_outcome_unknown":
                    if outcome_unknown_ack != OUTCOME_UNKNOWN_ACK:
                        raise AdvisorError(
                            "replaying unknown-submission abandonment requires "
                            "the exact outcome-unknown acknowledgement"
                        )
                    if question_sha256 != row["question_sha256"]:
                        raise AdvisorConflict(
                            "unknown-submission abandonment must bind the exact question"
                        )
                elif outcome_unknown_ack is not None or question_sha256 is not None:
                    raise AdvisorError(
                        "outcome-unknown acknowledgement is valid only for "
                        "submission_unknown"
                    )
                connection.commit()
                return self.status(request_id)
            if row["state"] not in {
                "prepared",
                "authorized",
                "submission_unknown",
            }:
                raise AdvisorError(
                    f"advisor request cannot be abandoned from state {row['state']}"
                )
            receipt_sha: str | None = None
            unknown_outcome = row["state"] == "submission_unknown"
            self._discard_uncommitted_terminal_receipt(request_id)
            if unknown_outcome:
                if outcome_unknown_ack != OUTCOME_UNKNOWN_ACK:
                    raise AdvisorError(
                        "abandoning an unknown submission requires the exact "
                        "outcome-unknown acknowledgement"
                    )
                if question_sha256 != row["question_sha256"]:
                    raise AdvisorConflict(
                        "unknown-submission abandonment must bind the exact question"
                    )
                receipt = _abandoned_unknown_receipt_payload(row, reason=reason)
                raw = (_canonical_json(receipt) + "\n").encode("utf-8")
                receipt_sha = _sha256_bytes(raw)
            elif outcome_unknown_ack is not None or question_sha256 is not None:
                raise AdvisorError(
                    "outcome-unknown acknowledgement is valid only for "
                    "submission_unknown"
                )
            self._append_event(
                connection,
                request_id=request_id,
                kind="advisor_request_abandoned",
                actor="owner",
                payload={
                    "outcome_unknown": unknown_outcome,
                    "prior_state": row["state"],
                    "reason": reason,
                    "receipt_sha256": receipt_sha,
                },
            )
            connection.execute(
                "UPDATE jobs SET state = ?, receipt_sha256 = ?, receipt_published = 0, "
                "outcome_unknown_abandoned = ?, terminal_reason = ?, "
                "updated_at_utc = ? "
                "WHERE request_id = ?",
                (
                    (
                        "owner_abandoned_outcome_unknown"
                        if unknown_outcome
                        else "abandoned"
                    ),
                    receipt_sha,
                    int(unknown_outcome),
                    reason,
                    _utc_now(),
                    request_id,
                ),
            )
            connection.commit()
        return self.status(request_id)

    def status(self, request_id: str) -> dict[str, Any]:
        self._publish_pending_terminal_receipt(request_id)
        with self._connect() as connection:
            row = self._job(connection, request_id)
            self._attest_lineage(connection, row)
        self._verify_terminal_receipt(row)
        return {
            "answer_bytes": row["answer_bytes"],
            "answer_sha256": row["answer_sha256"],
            "authorization_id": row["authorization_id"],
            "conversation_url_sha256": row["conversation_url_sha256"],
            "delivery_attempt_count": row["delivery_attempt_count"],
            "delivery_client_message_id": row["delivery_client_message_id"],
            "delivery_mode": row["delivery_mode"],
            "dispatch_count": row["dispatch_count"],
            "lineage": _lineage_payload(row),
            "lineage_kind": row["lineage_kind"],
            "problem_id": row["problem_id"],
            "question_bytes": row["question_bytes"],
            "question_sha256": row["question_sha256"],
            "receipt_sha256": row["receipt_sha256"],
            "report_receipt_sha256": row["report_receipt_sha256"],
            "request_id": row["request_id"],
            "run_id": row["run_id"],
            "source_kind": SOURCE_KIND,
            "state": row["state"],
            "terminal_reason": row["terminal_reason"],
            "transport": TRANSPORT,
            "ui_mode": UI_MODE,
            "usage": None,
            "cost_usd": None,
            "billing_basis": "subscription",
            "clarification": row["clarification"],
            "clarification_bytes": row["clarification_bytes"],
            "clarification_sha256": row["clarification_sha256"],
        }

    def events(self, request_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            self._job(connection, request_id)
            rows = connection.execute(
                "SELECT * FROM events WHERE request_id = ? ORDER BY sequence",
                (request_id,),
            ).fetchall()
        return [
            {
                "actor": row["actor"],
                "created_at_utc": row["created_at_utc"],
                "digest": row["digest"],
                "event_id": row["event_id"],
                "kind": row["kind"],
                "payload": json.loads(row["payload_json"]),
                "previous_digest": row["previous_digest"],
                "request_id": row["request_id"],
                "sequence": row["sequence"],
            }
            for row in rows
        ]

    def verify_chain(self) -> dict[str, Any]:
        with self._connect() as connection:
            request_ids = [
                str(row["request_id"])
                for row in connection.execute(
                    "SELECT request_id FROM jobs ORDER BY request_id"
                ).fetchall()
            ]
        for request_id in request_ids:
            self._publish_pending_terminal_receipt(request_id)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM events ORDER BY sequence"
            ).fetchall()
            head = connection.execute(
                "SELECT value FROM metadata WHERE key = 'head_digest'"
            ).fetchone()
            jobs = connection.execute(
                "SELECT * FROM jobs ORDER BY request_id"
            ).fetchall()
        previous = ZERO_DIGEST
        for row in rows:
            if row["previous_digest"] != previous:
                raise AdvisorError(
                    f"advisor event chain mismatch at sequence {row['sequence']}"
                )
            material = {
                "actor": row["actor"],
                "created_at_utc": row["created_at_utc"],
                "event_id": row["event_id"],
                "kind": row["kind"],
                "payload": json.loads(row["payload_json"]),
                "previous_digest": row["previous_digest"],
                "request_id": row["request_id"],
            }
            digest = _sha256_text(_canonical_json(material))
            if digest != row["digest"]:
                raise AdvisorError(
                    f"advisor event digest mismatch at sequence {row['sequence']}"
                )
            previous = digest
        if head is None or str(head["value"]) != previous:
            raise AdvisorError("advisor event head digest mismatch")
        for job in jobs:
            with self._connect() as connection:
                current = self._job(connection, str(job["request_id"]))
                self._attest_lineage(connection, current)
            self._verify_terminal_receipt(job)
        return {"event_count": len(rows), "head_digest": previous, "valid": True}


def _read_text_argument(args: argparse.Namespace, name: str) -> str:
    direct = getattr(args, name, None)
    path = getattr(args, f"{name}_file", None)
    use_stdin = bool(getattr(args, f"{name}_stdin", False))
    selected = sum(
        value is not None and value is not False for value in (direct, path, use_stdin)
    )
    if selected != 1:
        raise ValueError(
            f"provide exactly one --{name}, --{name}-file, or --{name}-stdin"
        )
    if direct is not None:
        return str(direct)
    if path is not None:
        return Path(path).read_text(encoding="utf-8")
    return sys.stdin.read()


def _add_text_source(parser: argparse.ArgumentParser, name: str) -> None:
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(f"--{name}")
    group.add_argument(f"--{name}-file", type=Path)
    group.add_argument(f"--{name}-stdin", action="store_true")


def _add_optional_text_source(parser: argparse.ArgumentParser, name: str) -> None:
    group = parser.add_mutually_exclusive_group(required=False)
    group.add_argument(f"--{name}")
    group.add_argument(f"--{name}-file", type=Path)
    group.add_argument(f"--{name}-stdin", action="store_true")


def _add_optional_file_or_stdin_source(
    parser: argparse.ArgumentParser, name: str
) -> None:
    """Add a transient sensitive input without an argv plaintext form."""

    group = parser.add_mutually_exclusive_group(required=False)
    group.add_argument(f"--{name}-file", type=Path)
    group.add_argument(f"--{name}-stdin", action="store_true")


def _add_file_or_stdin_source(parser: argparse.ArgumentParser, name: str) -> None:
    """Require one transient sensitive input without an argv plaintext form."""

    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(f"--{name}-file", type=Path)
    group.add_argument(f"--{name}-stdin", action="store_true")


def _read_optional_text_argument(args: argparse.Namespace, name: str) -> str | None:
    direct = getattr(args, name, None)
    path = getattr(args, f"{name}_file", None)
    use_stdin = bool(getattr(args, f"{name}_stdin", False))
    if direct is None and path is None and not use_stdin:
        return None
    return _read_text_argument(args, name)


def _read_optional_owner_file_or_stdin(
    args: argparse.Namespace, name: str
) -> str | None:
    """Read a transient conversation URL without argv or unsafe file aliases."""

    path = getattr(args, f"{name}_file", None)
    use_stdin = bool(getattr(args, f"{name}_stdin", False))
    if path is None and not use_stdin:
        return None
    if path is not None and use_stdin:  # argparse also enforces this.
        raise ValueError(f"provide only one --{name}-file or --{name}-stdin")
    if use_stdin:
        return sys.stdin.read()
    candidate = Path(path)
    try:
        before = candidate.lstat()
    except OSError as exc:
        raise AdvisorError(f"{name} file cannot be inspected safely") from exc
    if not stat.S_ISREG(before.st_mode) or candidate.is_symlink():
        raise AdvisorError(f"{name} file must be a regular non-symlink file")
    if hasattr(os, "getuid") and before.st_uid != os.getuid():
        raise AdvisorError(f"{name} file belongs to another local user")
    if stat.S_IMODE(before.st_mode) & 0o077:
        raise AdvisorError(f"{name} file must be owner-only")
    if before.st_size > 4096:
        raise AdvisorError(f"{name} file exceeds 4096 bytes")
    descriptor = -1
    try:
        descriptor = os.open(
            candidate,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        )
        after = os.fstat(descriptor)
        if (
            not stat.S_ISREG(after.st_mode)
            or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
            or after.st_nlink != 1
            or after.st_uid != before.st_uid
            or stat.S_IMODE(after.st_mode) & 0o077
            or after.st_size > 4096
        ):
            raise AdvisorError(f"{name} file changed during secure open")
        raw = os.read(descriptor, 4097)
        if len(raw) > 4096 or os.read(descriptor, 1):
            raise AdvisorError(f"{name} file exceeds 4096 bytes")
    except OSError as exc:
        raise AdvisorError(f"{name} file cannot be read safely") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    try:
        return raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{name} file must be valid UTF-8") from exc


def _read_owner_file_or_stdin(args: argparse.Namespace, name: str) -> str:
    value = _read_optional_owner_file_or_stdin(args, name)
    if value is None:
        raise ValueError(f"provide --{name}-file or --{name}-stdin")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AxiomRelay Chrome-only advisor broker")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--receipts-root", type=Path, default=DEFAULT_RECEIPTS_ROOT)
    commands = parser.add_subparsers(dest="command", required=True)

    prepare = commands.add_parser("prepare")
    prepare.add_argument("--request-id")
    prepare.add_argument("--run-id", required=True)
    prepare.add_argument("--problem-id", required=True)
    prepare.add_argument("--query-skill-sha256", required=True)
    prepare.add_argument("--computer-use-skill-sha256", required=True)
    prepare.add_argument("--predecessor-request-id")
    prepare.add_argument("--external-source-repo")
    prepare.add_argument("--external-request-id")
    prepare.add_argument("--external-receipt-sha256")
    prepare.add_argument("--external-source-context-sha256")
    prepare.add_argument("--external-conversation-url-sha256")
    prepare.add_argument("--external-owner-ack")
    _add_optional_file_or_stdin_source(prepare, "external-conversation-url")
    _add_text_source(prepare, "question")

    authorize = commands.add_parser("authorize")
    authorize.add_argument("--request-id", required=True)
    authorize.add_argument("--authorization-id", required=True)
    authorize.add_argument("--question-sha256", required=True)

    dispatch = commands.add_parser("begin-dispatch")
    dispatch.add_argument("--request-id", required=True)
    _add_optional_file_or_stdin_source(dispatch, "conversation-url")

    submitted = commands.add_parser("submitted")
    submitted.add_argument("--request-id", required=True)
    submitted.add_argument("--ui-mode", required=True)
    _add_file_or_stdin_source(submitted, "conversation-url")
    _add_text_source(submitted, "observed-question")

    recover = commands.add_parser("recover-submitted")
    recover.add_argument("--request-id", required=True)
    recover.add_argument("--ui-mode", required=True)
    _add_file_or_stdin_source(recover, "conversation-url")
    _add_text_source(recover, "observed-question")

    unknown = commands.add_parser("submission-unknown")
    unknown.add_argument("--request-id", required=True)
    unknown.add_argument("--reason", required=True)
    _add_optional_file_or_stdin_source(unknown, "conversation-url")

    failed = commands.add_parser("failed-not-submitted")
    failed.add_argument("--request-id", required=True)
    failed.add_argument("--reason", required=True)
    failed.add_argument("--send-not-clicked-confirmed", action="store_true")

    complete = commands.add_parser("complete")
    complete.add_argument("--request-id", required=True)
    _add_text_source(complete, "answer")
    complete.add_argument("--answer-snapshot-a-sha256", required=True)
    complete.add_argument("--answer-snapshot-b-sha256", required=True)
    complete.add_argument("--ui-mode", required=True)
    complete.add_argument("--response-actions-present", action="store_true")
    complete.add_argument("--composer-available", action="store_true")
    complete.add_argument("--working-indicators-absent", action="store_true")

    clarification = commands.add_parser("needs-user-input")
    clarification.add_argument("--request-id", required=True)
    _add_text_source(clarification, "clarification")

    imported = commands.add_parser("import")
    imported.add_argument("--request-id", required=True)
    imported.add_argument("--hotjoin-db", type=Path, required=True)
    imported.add_argument("--mode", choices=("steer",), default="steer")
    _add_optional_text_source(imported, "answer")

    retry = commands.add_parser("retry-delivery")
    retry.add_argument("--request-id", required=True)
    retry.add_argument("--hotjoin-db", type=Path, required=True)
    retry.add_argument("--mode", choices=("steer",), required=True)

    abandon = commands.add_parser("abandon")
    abandon.add_argument("--request-id", required=True)
    abandon.add_argument("--reason", required=True)
    abandon.add_argument("--outcome-unknown-ack")
    abandon.add_argument("--question-sha256")

    status = commands.add_parser("status")
    status.add_argument("--request-id", required=True)

    commands.add_parser("verify-ledger")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        ledger = AdvisorLedger(args.db, receipts_root=args.receipts_root)
        if args.command == "prepare":
            result = ledger.prepare(
                request_id=args.request_id,
                run_id=args.run_id,
                problem_id=args.problem_id,
                question=_read_text_argument(args, "question"),
                query_skill_sha256=args.query_skill_sha256,
                computer_use_skill_sha256=args.computer_use_skill_sha256,
                predecessor_request_id=args.predecessor_request_id,
                external_source_repo=args.external_source_repo,
                external_request_id=args.external_request_id,
                external_receipt_sha256=args.external_receipt_sha256,
                external_source_context_sha256=(args.external_source_context_sha256),
                external_conversation_url_sha256=(
                    args.external_conversation_url_sha256
                ),
                external_owner_ack=args.external_owner_ack,
                external_conversation_url=_read_optional_owner_file_or_stdin(
                    args, "external_conversation_url"
                ),
            )
        elif args.command == "authorize":
            result = ledger.authorize(
                args.request_id,
                authorization_id=args.authorization_id,
                question_sha256=args.question_sha256,
            )
        elif args.command == "begin-dispatch":
            result = ledger.begin_dispatch(
                args.request_id,
                conversation_url=_read_optional_owner_file_or_stdin(
                    args, "conversation_url"
                ),
            )
        elif args.command in {"submitted", "recover-submitted"}:
            result = ledger.mark_submitted(
                args.request_id,
                conversation_url=_read_owner_file_or_stdin(args, "conversation_url"),
                observed_question=_read_text_argument(args, "observed_question"),
                ui_mode=args.ui_mode,
                recover_unknown=args.command == "recover-submitted",
            )
        elif args.command == "submission-unknown":
            result = ledger.mark_submission_unknown(
                args.request_id,
                reason=args.reason,
                conversation_url=_read_optional_owner_file_or_stdin(
                    args, "conversation_url"
                ),
            )
        elif args.command == "failed-not-submitted":
            result = ledger.failed_not_submitted(
                args.request_id,
                reason=args.reason,
                send_not_clicked_confirmed=args.send_not_clicked_confirmed,
            )
        elif args.command == "complete":
            result = ledger.complete(
                args.request_id,
                answer=_read_text_argument(args, "answer"),
                answer_snapshot_a_sha256=args.answer_snapshot_a_sha256,
                answer_snapshot_b_sha256=args.answer_snapshot_b_sha256,
                ui_mode=args.ui_mode,
                response_actions_present=args.response_actions_present,
                composer_available=args.composer_available,
                working_indicators_absent=args.working_indicators_absent,
            )
        elif args.command == "needs-user-input":
            result = ledger.needs_user_input(
                args.request_id,
                clarification=_read_text_argument(args, "clarification"),
            )
        elif args.command in {"import", "retry-delivery"}:
            result = ledger.import_report(
                args.request_id,
                hotjoin_db=args.hotjoin_db,
                mode=args.mode,
                retry_unknown=args.command == "retry-delivery",
                answer=(
                    _read_optional_text_argument(args, "answer")
                    if args.command == "import"
                    else None
                ),
            )
        elif args.command == "abandon":
            result = ledger.abandon(
                args.request_id,
                reason=args.reason,
                outcome_unknown_ack=args.outcome_unknown_ack,
                question_sha256=args.question_sha256,
            )
        elif args.command == "status":
            result = ledger.status(args.request_id)
        elif args.command == "verify-ledger":
            result = ledger.verify_chain()
        else:  # pragma: no cover
            raise AssertionError("unreachable")
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except (AdvisorError, OSError, UnicodeError, ValueError, sqlite3.Error) as exc:
        print(f"rethlas advisor error: {_redacted_reason(str(exc))}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
