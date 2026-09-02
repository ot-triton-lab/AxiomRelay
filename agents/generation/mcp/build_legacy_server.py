#!/usr/bin/env python3
"""Generate the byte-isolated Legacy MCP and verification runtime modules."""

from __future__ import annotations

import argparse
import ast
import builtins
import hashlib
import symtable
import sys
from pathlib import Path
from typing import Iterable


ROOT_EXPORTS = frozenset(
    {
        "search_matlas_theorems",
        "search_arxiv_theorems",
        "search_arxiv_theorems_for_problem",
        "read_arxiv_primary_for_problem",
        "append_route_terminal_report",
        "verify_blueprint_service",
        "memory_init",
        "memory_append",
        "memory_append_batch",
        "memory_search",
        "branch_update",
        "generation_control_resume",
        "generation_control_status",
        "generation_control_receipt",
        "_exact_checkpoint_tool_result",
        "_checkpoint_failure_tool_result",
    }
)

PROVIDED_NAMES = frozenset(
    {
        "FastMCP",
        "CallToolResult",
        "TextContent",
        "ProofParseError",
        "PublicationProofParseError",
        "CHANNEL_FILES",
        "_CONTROL_ONLY_MEMORY_CHANNELS",
        "canonical_json_bytes",
        "verify_blueprint_file",
        "_released_memory_registry_configured",
        "_reasoning_phase_preflight",
    }
)

BLOCKED_NAMES = frozenset(
    {
        "advisor_report_get",
        "apply_effective_verdict",
        "build_review_request",
        "build_targeted_verification_ticket",
        "trusted_handoff_id",
        "handoff_sha256",
        "validate_context_handoff",
        "validate_review_report",
        "validate_targeted_verification_ticket",
        "verify_targeted_claim_service",
        "_validate_memory_batch_publication_status_snapshot",
    }
)

FUNCTION_REPLACEMENTS = {
    "_iter_memory_batch_checkpoints": '''\
def _iter_memory_batch_checkpoints(
    problem_id: str,
    *,
    owner_manifest_snapshot_json: str | None = None,
) -> Iterable[Dict[str, Any]]:
    """Read only local committed checkpoints and compatible legacy batches."""

    if owner_manifest_snapshot_json is not None:
        raise ValueError("legacy MCP rejects owner publication snapshots")
    checkpoint_dir = _batch_checkpoint_dir(problem_id)
    try:
        descriptor = _open_memory_directory(checkpoint_dir, create=False)
    except FileNotFoundError:
        return
    try:
        names = sorted(
            name
            for name in os.listdir(descriptor)
            if name.startswith("batch_")
            and name.endswith(".json")
            and not name.endswith(".commit.json")
        )
        _validate_directory_metadata(
            os.fstat(descriptor),
            label=f"memory checkpoint directory {checkpoint_dir}",
            require_owner=True,
        )
    finally:
        os.close(descriptor)
    for name in names:
        path = checkpoint_dir / name
        checkpoint = _validate_memory_batch_checkpoint_data(problem_id, path)
        commit = _read_memory_batch_commit(checkpoint, path, allow_missing=True)
        if commit is None:
            if checkpoint["schema"] == LEGACY_MEMORY_BATCH_SCHEMA:
                yield {
                    **checkpoint,
                    "committed_at_utc": checkpoint["timestamp_utc"],
                    "committed_at_monotonic": None,
                    "commit_sha256": None,
                    "legacy_unmarked": True,
                }
            continue
        yield {
            **checkpoint,
            "committed_at_utc": commit["committed_at_utc"],
            "committed_at_monotonic": commit["committed_at_monotonic"],
            "commit_sha256": commit["commit_sha256"],
            "publication_receipt": None,
            "legacy_unmarked": False,
        }
''',
    "_memory_batch_receipt": '''\
def _memory_batch_receipt(
    problem_id: str,
    checkpoint_path: Path,
    committed: Mapping[str, Any],
) -> Dict[str, Any]:
    """Return the exact local-commit receipt supported by Legacy mode."""

    if committed.get("publication_receipt") is not None:
        raise ValueError("legacy MCP rejects host publication receipts")
    receipts = [
        {
            "record_id": entry["record_id"],
            "channel": entry["channel"],
            "active": entry["active"],
            "supersedes": entry["supersedes"],
        }
        for entry in committed["records"]
    ]
    return {
        "schema_version": MEMORY_BATCH_LOCAL_COMMIT_RECEIPT_SCHEMA,
        "status": "ok",
        "problem_id": problem_id,
        "batch_id": committed["batch_id"],
        "timestamp_utc": committed["timestamp_utc"],
        "committed_at_utc": committed["committed_at_utc"],
        "committed_at_monotonic": committed["committed_at_monotonic"],
        "commit_sha256": committed["commit_sha256"],
        "checkpoint_sha256": committed["checkpoint_sha256"],
        "count": len(receipts),
        "records": receipts,
        "checkpoint_path": str(checkpoint_path),
    }
''',
}

REPLACED_SOURCE_SHA256 = {
    "_iter_memory_batch_checkpoints": (
        "d74b33870b71e8c6d434861f0a85c938a1bd3e0f134b1684f6044a8fb1e49c66"
    ),
    "_memory_batch_receipt": (
        "1a02f31101cb5a3e3b9b04d1e96018eba42d8dcb90f37e51b7a07a2d781dcbaf"
    ),
}

SOURCE_SIGNATURES = {
    "search_matlas_theorems": (
        "query: str, num_results: int=10, endpoint: str=MATLAS_SEARCH_URL, "
        "timeout_seconds: int=30"
    ),
    "search_arxiv_theorems": (
        "query: str, num_results: int=10, "
        "endpoint: str=LEGACY_ARXIV_THEOREM_URL, timeout_seconds: int=30"
    ),
    "search_arxiv_theorems_for_problem": (
        "problem_id: str, query: str, num_results: int=10, "
        "expected_statement_sha256: Optional[str]=None"
    ),
    "read_arxiv_primary_for_problem": (
        "problem_id: str, arxiv_id: str, locator: str, "
        "max_excerpt_bytes: int=20000, "
        "expected_statement_sha256: Optional[str]=None"
    ),
    "append_route_terminal_report": (
        "problem_id: str, thread_id: str, plan_id: str, status: str, "
        "report_text: str, remaining_obligations: List[str], "
        "decisive_stuck_points: List[str]"
    ),
    "verify_blueprint_service": (
        "problem_id: str, endpoint: str=VERIFY_PROOF_URL, "
        "timeout_seconds: int=3600"
    ),
    "memory_init": "problem_id: str, meta: Optional[Dict[str, Any]]=None",
    "memory_append": (
        "problem_id: str, channel: str, record: Dict[str, Any], "
        "active: bool=True, supersedes: Optional[List[str]]=None, "
        "return_mode: str='metadata'"
    ),
    "memory_append_batch": (
        "problem_id: str, items: List[Dict[str, Any]], *, "
        "_trusted_control_publication: bool=False, "
        "_trusted_publication_preflight: "
        "Callable[[], Mapping[str, Any] | None] | None=None"
    ),
    "memory_search": (
        "problem_id: str, query: str, channels: Optional[List[str]]=None, "
        "limit_per_channel: int=10, "
        "max_chars: int=DEFAULT_MEMORY_SEARCH_MAX_CHARS, "
        "include_inactive: bool=False, newest_first: bool=True"
    ),
    "branch_update": "problem_id: str, branch_id: str, state: Dict[str, Any]",
    "generation_control_resume": "problem_id: str, instance_id: str",
    "generation_control_status": (
        "problem_id: str, instance_id: str, *, "
        "owner_manifest_snapshot_json: str | None=None"
    ),
    "generation_control_receipt": (
        "problem_id: str, instance_id: str, *, "
        "owner_manifest_snapshot_json: str | None=None"
    ),
    "_exact_checkpoint_tool_result": "receipt: Dict[str, Any]",
    "_checkpoint_failure_tool_result": "exc: MemoryCheckpointPreflightError",
}

EXPECTED_FULL_CHANNELS = {
    "immediate_conclusions": "immediate_conclusions.jsonl",
    "toy_examples": "toy_examples.jsonl",
    "counterexamples": "counterexamples.jsonl",
    "big_decisions": "big_decisions.jsonl",
    "subgoals": "subgoals.jsonl",
    "proof_steps": "proof_steps.jsonl",
    "failed_paths": "failed_paths.jsonl",
    "verification_reports": "verification_reports.jsonl",
    "route_reviews": "route_reviews.jsonl",
    "targeted_verifications": "targeted_verifications.jsonl",
    "branch_states": "branch_states.jsonl",
    "events": "events.jsonl",
}

HEADER = '''\
"""Generated byte-isolated MCP server for cadence-disabled Legacy runs.

Do not edit this file directly. Run ``python mcp/build_legacy_server.py --write``.
"""

from __future__ import annotations

import fcntl
import hashlib
import hmac
import html
import json
import math
import os
import re
import stat
import sys
import time
import unicodedata
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional

import requests

if os.getenv("RETHLAS_RUNTIME_PROFILE") != "legacy":
    raise RuntimeError("legacy_server.py requires RETHLAS_RUNTIME_PROFILE=legacy")

_FORBIDDEN_CONTROL_ENV = frozenset(
    {
        "RETHLAS_HOTJOIN_RUN_ID",
        "RETHLAS_EXPECTED_HOTJOIN_RUN_ID",
        "RETHLAS_ADVISOR_RECEIPTS_ROOT",
        "RETHLAS_REVIEW_CADENCE_POLICY",
        "RETHLAS_CONTEXT_GUARD_POLICY",
        "RETHLAS_POLICY_CONTRACT_SHA256",
        "RETHLAS_REVIEW_ADAPTER_PATH",
        "RETHLAS_REVIEW_ADAPTER_SHA256",
        "RETHLAS_REVIEW_DB",
        "RETHLAS_REVIEW_CONTROL_TOKEN",
        "RETHLAS_REVIEW_EXPECTED_MODEL",
        "RETHLAS_REVIEW_EXPECTED_REASONING_EFFORT",
        "RETHLAS_REVIEW_POLICY_SHA256",
        "RETHLAS_REVIEW_CONTRACT_CLI_PATH",
        "RETHLAS_REVIEW_CONTRACT_CLI_SHA256",
        "RETHLAS_CONTEXT_HANDOFF_REQUIRED_ID",
        "RETHLAS_CONTEXT_HANDOFF_REQUIRED_SHA256",
        "RETHLAS_CONTEXT_THREAD_EPOCH",
        "RETHLAS_CONTEXT_CYCLE_ID",
        "RETHLAS_CONTEXT_RUN_ID",
        "RETHLAS_GUARDIAN_CYCLE_TOKEN",
        "RETHLAS_RUNNER_CYCLE_TOKEN",
        "RETHLAS_STALE_RECOVERY_TOKEN",
        "RETHLAS_OWNER_MEMORY_BATCH_PUBLICATION_SNAPSHOT_JSON",
        "RETHLAS_COST_GATE_POLICY",
        "RETHLAS_RESOLVED_COST_POLICY_JSON",
        "RETHLAS_RESOLVED_COST_POLICY_SHA256",
        "RETHLAS_NONFRESH_RESUME_DRY_RUN",
        "RETHLAS_NONFRESH_STALE_RECONCILE",
        "RETHLAS_NONFRESH_RESUME_DB_COPY",
    }
)
_leaked = sorted(_FORBIDDEN_CONTROL_ENV & set(os.environ))
if _leaked:
    raise RuntimeError(
        "legacy MCP server received continuous control bindings: "
        + ", ".join(_leaked)
    )

try:
    from mcp.server.fastmcp import FastMCP
except ImportError:
    try:
        from mcp.server.mcpserver import MCPServer as FastMCP
    except ImportError:
        FastMCP = None

try:
    from mcp.types import CallToolResult, TextContent
except ImportError:
    CallToolResult = None
    TextContent = None

if __package__ in {None, ""}:
    _ATTESTED_MCP_ROOT = Path(__file__).resolve(strict=True).parent
    sys.path.insert(0, str(_ATTESTED_MCP_ROOT))

try:
    from .legacy_verification_client import verify_blueprint_file
except ImportError:
    from legacy_verification_client import verify_blueprint_file

try:
    from .proof_context import ProofParseError
except ImportError:
    from proof_context import ProofParseError

try:
    from .publication_proof_context_v3 import (
        ProofParseError as PublicationProofParseError,
    )
except ImportError:
    from publication_proof_context_v3 import (
        ProofParseError as PublicationProofParseError,
    )


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


_BOUND_EXTERNAL_PLAN_ENV = (
    "RETHLAS_BOUND_EXTERNAL_PLAN_PATH",
    "RETHLAS_BOUND_EXTERNAL_PLAN_SHA256",
    "RETHLAS_BOUND_EXTERNAL_PLAN_ROOT_SESSION_ID",
)
_BOUND_EXTERNAL_PLAN_SCHEMA = "rethlas_claude_plan_set_v1"
_BOUND_EXTERNAL_PLAN_KEYS = {
    "schema_version",
    "problem_id",
    "statement_sha256",
    "root_session_id",
    "plans",
}
_BOUND_EXTERNAL_ROUTE_KEYS = {
    "plan_id",
    "mechanism",
    "scope",
    "discriminating_test",
    "plan_summary",
    "subgoals",
    "motivation",
}


def _bound_external_plan_failure(message: str) -> None:
    raise MemoryCheckpointPreflightError(message, retry_allowed=False)


def _bound_external_plan_text(value: Any, *, label: str, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > maximum
    ):
        _bound_external_plan_failure(f"{label} is invalid")
    return value


def _bound_external_plan_text_list(
    value: Any, *, label: str
) -> list[str]:
    if not isinstance(value, list) or not 1 <= len(value) <= 12:
        _bound_external_plan_failure(f"{label} is invalid")
    normalized = [
        _bound_external_plan_text(
            item, label=f"{label}[{index}]", maximum=2048
        )
        for index, item in enumerate(value)
    ]
    return normalized


def _secure_read_bound_external_plan(
    path_value: str, expected_sha256: str
) -> bytes:
    if (
        not isinstance(path_value, str)
        or not os.path.isabs(path_value)
        or os.path.abspath(path_value) != path_value
        or "\\x00" in path_value
        or re.fullmatch(r"[0-9a-f]{64}", expected_sha256 or "") is None
    ):
        _bound_external_plan_failure("bound external plan identity is invalid")
    path = Path(path_value)
    try:
        relative = path.relative_to(REPO_ROOT)
        resolved = path.resolve(strict=True)
        before = path.lstat()
    except (OSError, ValueError) as exc:
        _bound_external_plan_failure(
            "bound external plan cannot be resolved: " + str(exc)
        )
    if (
        not relative.parts
        or relative.parts[0] != ".claude_core_inputs"
        or resolved != path
        or path.name != f"plan_{expected_sha256}.json"
        or path.is_symlink()
        or not stat.S_ISREG(before.st_mode)
        or stat.S_IMODE(before.st_mode) & 0o222
        or before.st_nlink != 1
        or before.st_size < 1
        or before.st_size > 65_536
        or before.st_uid not in {0, os.geteuid()}
    ):
        _bound_external_plan_failure("bound external plan file is unsafe")
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError as exc:
        _bound_external_plan_failure(
            "bound external plan cannot be opened: " + str(exc)
        )
    try:
        opened = os.fstat(descriptor)
        identity = (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
            opened.st_mtime_ns,
        )
        if identity != (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ):
            _bound_external_plan_failure(
                "bound external plan changed during secure open"
            )
        chunks: list[bytes] = []
        remaining = int(opened.st_size)
        while remaining:
            chunk = os.read(descriptor, min(65_536, remaining))
            if not chunk:
                _bound_external_plan_failure(
                    "bound external plan produced a short read"
                )
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            _bound_external_plan_failure("bound external plan grew while read")
        after = os.fstat(descriptor)
        if identity != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            _bound_external_plan_failure(
                "bound external plan changed while read"
            )
    finally:
        os.close(descriptor)
    raw = b"".join(chunks)
    if not hmac.compare_digest(
        hashlib.sha256(raw).hexdigest(), expected_sha256
    ):
        _bound_external_plan_failure("bound external plan SHA-256 mismatch")
    return raw


def _materialize_bound_external_plan_checkpoint_items(
    problem_id: str, items: Any
) -> Any:
    """Expand an authenticated external plan only for the empty-list sentinel."""

    if not isinstance(items, list) or items:
        return items
    bindings = {name: os.getenv(name) for name in _BOUND_EXTERNAL_PLAN_ENV}
    if all(value is None for value in bindings.values()):
        return items
    if any(not isinstance(value, str) or not value for value in bindings.values()):
        _bound_external_plan_failure(
            "bound external plan environment is incomplete"
        )
    expected_problem_id = os.getenv("RETHLAS_EXPECTED_PROBLEM_ID")
    expected_statement_sha256 = os.getenv(
        "RETHLAS_EXPECTED_STATEMENT_SHA256"
    )
    if (
        problem_id != expected_problem_id
        or re.fullmatch(
            r"[0-9a-f]{64}", expected_statement_sha256 or ""
        )
        is None
    ):
        _bound_external_plan_failure(
            "bound external plan problem or statement binding is invalid"
        )
    raw = _secure_read_bound_external_plan(
        bindings["RETHLAS_BOUND_EXTERNAL_PLAN_PATH"],
        bindings["RETHLAS_BOUND_EXTERNAL_PLAN_SHA256"],
    )
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        _bound_external_plan_failure(
            "bound external plan is not strict JSON: " + str(exc)
        )
    if (
        not isinstance(value, dict)
        or set(value) != _BOUND_EXTERNAL_PLAN_KEYS
        or value.get("schema_version") != _BOUND_EXTERNAL_PLAN_SCHEMA
        or value.get("problem_id") != problem_id
        or value.get("statement_sha256") != expected_statement_sha256
        or value.get("root_session_id")
        != bindings["RETHLAS_BOUND_EXTERNAL_PLAN_ROOT_SESSION_ID"]
        or canonical_json_bytes(value) + b"\\n" != raw
    ):
        _bound_external_plan_failure(
            "bound external plan schema or canonical binding mismatch"
        )
    plans = value.get("plans")
    if not isinstance(plans, list) or len(plans) != 3:
        _bound_external_plan_failure(
            "bound external plan must contain exactly three plans"
        )
    normalized: list[dict[str, Any]] = []
    plan_ids: list[str] = []
    mechanisms: list[str] = []
    scopes: list[str] = []
    for index, plan in enumerate(plans):
        label = f"bound external plan[{index}]"
        if not isinstance(plan, dict) or set(plan) != _BOUND_EXTERNAL_ROUTE_KEYS:
            _bound_external_plan_failure(f"{label} has an unsupported shape")
        plan_id = _bound_external_plan_text(
            plan.get("plan_id"), label=f"{label}.plan_id", maximum=64
        )
        if re.fullmatch(r"[a-z][a-z0-9_]{0,63}", plan_id) is None:
            _bound_external_plan_failure(f"{label}.plan_id is invalid")
        mechanism = _bound_external_plan_text(
            plan.get("mechanism"), label=f"{label}.mechanism", maximum=2048
        )
        scope = _bound_external_plan_text(
            plan.get("scope"), label=f"{label}.scope", maximum=2048
        )
        normalized_plan = {
            "plan_id": plan_id,
            "mechanism": mechanism,
            "scope": scope,
            "discriminating_test": _bound_external_plan_text(
                plan.get("discriminating_test"),
                label=f"{label}.discriminating_test",
                maximum=4096,
            ),
            "plan_summary": _bound_external_plan_text(
                plan.get("plan_summary"),
                label=f"{label}.plan_summary",
                maximum=4096,
            ),
            "subgoals": _bound_external_plan_text_list(
                plan.get("subgoals"), label=f"{label}.subgoals"
            ),
            "motivation": _bound_external_plan_text_list(
                plan.get("motivation"), label=f"{label}.motivation"
            ),
        }
        if normalized_plan != plan:
            _bound_external_plan_failure(f"{label} is not normalized")
        normalized.append(normalized_plan)
        plan_ids.append(plan_id)
        mechanisms.append(
            " ".join(
                re.findall(
                    r"[\w]+",
                    unicodedata.normalize("NFKC", mechanism).casefold(),
                    flags=re.UNICODE,
                )
            )
        )
        scopes.append(
            " ".join(
                re.findall(
                    r"[\w]+",
                    unicodedata.normalize("NFKC", scope).casefold(),
                    flags=re.UNICODE,
                )
            )
        )
    if (
        len(set(plan_ids)) != 3
        or len(set(mechanisms)) != 3
        or len(set(scopes)) != 3
    ):
        _bound_external_plan_failure(
            "bound external plans are not exactly distinct"
        )
    return [
        {
            "channel": "subgoals",
            "record": plan,
            "active": True,
            "supersedes": [],
        }
        for plan in normalized
    ]


CHANNEL_FILES: Dict[str, str] = {
    "immediate_conclusions": "immediate_conclusions.jsonl",
    "toy_examples": "toy_examples.jsonl",
    "counterexamples": "counterexamples.jsonl",
    "big_decisions": "big_decisions.jsonl",
    "subgoals": "subgoals.jsonl",
    "proof_steps": "proof_steps.jsonl",
    "failed_paths": "failed_paths.jsonl",
    "verification_reports": "verification_reports.jsonl",
    "branch_states": "branch_states.jsonl",
    "events": "events.jsonl",
}
_CONTROL_ONLY_MEMORY_CHANNELS = frozenset()


def _released_memory_registry_configured(
    *, owner_manifest_snapshot_json: str | None = None
) -> bool:
    if owner_manifest_snapshot_json is not None:
        raise ValueError("legacy MCP rejects owner publication snapshots")
    return False


def _reasoning_phase_preflight(_tool_name: str) -> None:
    return None
'''

VERIFICATION_ROOT_EXPORTS = frozenset({"verify_blueprint_file"})
VERIFICATION_PROVIDED_NAMES = frozenset(
    {
        "ProofManifest",
        "aggregate_adaptive_context_digest",
        "aggregate_context_digest",
        "build_item_context",
        "extract_verification_target",
        "parse_blueprint",
    }
)
VERIFICATION_FORBIDDEN_DEFINITIONS = frozenset(
    {
        "_parse_targeted_manifest",
        "validate_targeted_claim_receipt",
        "verify_targeted_claim_service",
    }
)

VERIFICATION_HEADER = '''\
"""Generated whole-blueprint verification client for Legacy runs.

Do not edit this file directly. Run ``python mcp/build_legacy_server.py --write``.
"""

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

if os.getenv("RETHLAS_RUNTIME_PROFILE") != "legacy":
    raise RuntimeError(
        "legacy_verification_client.py requires RETHLAS_RUNTIME_PROFILE=legacy"
    )

if __package__ in {None, ""}:
    _ATTESTED_MCP_ROOT = Path(__file__).resolve(strict=True).parent
    sys.path.insert(0, str(_ATTESTED_MCP_ROOT))

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
except ImportError:
    from publication_proof_context_v3 import (
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
'''

FOOTER = '''\

LEGACY_FRONTIER_SCHEMA = "rethlas_legacy_frontier_receipt_v1"
MAX_LEGACY_FRONTIER_BLUEPRINT_BYTES = 8_000_000


def _legacy_blueprint_commitment(problem_id: str) -> Optional[Dict[str, Any]]:
    results_root = Path(os.path.abspath(os.fspath(RESULTS_ROOT)))
    candidate = results_root.joinpath(*problem_id.split("/"), "blueprint.md")
    try:
        metadata = candidate.lstat()
    except FileNotFoundError:
        return None
    if (
        candidate.is_symlink()
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_size < 1
        or metadata.st_size > MAX_LEGACY_FRONTIER_BLUEPRINT_BYTES
    ):
        raise ValueError("legacy frontier blueprint is not a bounded regular file")
    resolved_results = results_root.resolve(strict=True)
    resolved_candidate = candidate.resolve(strict=True)
    if (
        not resolved_candidate.is_relative_to(resolved_results)
        or resolved_candidate != candidate.absolute()
    ):
        raise ValueError("legacy frontier blueprint escaped the results root")
    descriptor = os.open(
        candidate,
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        opened = os.fstat(descriptor)
        identity = (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
            opened.st_mtime_ns,
        )
        if identity != (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_size,
            metadata.st_mtime_ns,
        ):
            raise ValueError("legacy frontier blueprint changed during open")
        remaining = int(opened.st_size)
        chunks: List[bytes] = []
        while remaining:
            chunk = os.read(descriptor, min(65_536, remaining))
            if not chunk:
                raise ValueError("legacy frontier blueprint produced a short read")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise ValueError("legacy frontier blueprint grew during read")
        after = os.fstat(descriptor)
        if identity != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise ValueError("legacy frontier blueprint changed during read")
    finally:
        os.close(descriptor)
    raw = b"".join(chunks)
    return {
        "sha256": hashlib.sha256(raw).hexdigest(),
        "bytes": len(raw),
    }


def _legacy_frontier_receipt(
    problem_id: str, *, excluded_record_ids: frozenset[str]
) -> Dict[str, Any]:
    normalized_problem_id = validate_verified_problem_id(problem_id)
    _statement, statement_sha256 = _trusted_problem_statement(normalized_problem_id)
    entries_by_channel = _load_memory_entries(normalized_problem_id)
    memory_records = []
    found_exclusions: set[str] = set()
    for channel in CHANNEL_FILES:
        for entry in entries_by_channel[channel]:
            record_id = str(entry["record_id"])
            if record_id in excluded_record_ids:
                found_exclusions.add(record_id)
                continue
            memory_records.append(
                {
                    "channel": channel,
                    "record_id": record_id,
                    "effective_active": bool(entry["effective_active"]),
                    "item_sha256": hashlib.sha256(
                        canonical_json_bytes(entry["item"])
                    ).hexdigest(),
                }
            )
    if found_exclusions != set(excluded_record_ids):
        raise ValueError("legacy frontier exclusion does not name exact records")
    memory_sha256 = hashlib.sha256(
        canonical_json_bytes(memory_records)
    ).hexdigest()
    body = {
        "schema_version": LEGACY_FRONTIER_SCHEMA,
        "problem_id": normalized_problem_id,
        "statement_sha256": statement_sha256,
        "blueprint": _legacy_blueprint_commitment(normalized_problem_id),
        "memory_sha256": memory_sha256,
        "memory_record_count": len(memory_records),
    }
    return {
        **body,
        "frontier_sha256": hashlib.sha256(canonical_json_bytes(body)).hexdigest(),
    }


def legacy_frontier_receipt(problem_id: str) -> Dict[str, Any]:
    return _legacy_frontier_receipt(problem_id, excluded_record_ids=frozenset())


def legacy_frontier_receipt_without_records(
    problem_id: str, record_ids: List[str]
) -> Dict[str, Any]:
    """Reconstruct a prior frontier after excluding one exact trusted batch.

    This helper is host-only recovery evidence.  It never mutates memory and
    rejects duplicate, malformed, missing, or non-content-addressed ids.
    """

    if (
        not isinstance(record_ids, list)
        or not 1 <= len(record_ids) <= MAX_MEMORY_BATCH_RECORDS
        or any(
            not isinstance(record_id, str)
            or re.fullmatch(r"(?:mem|event)_[0-9a-f]{64}", record_id) is None
            for record_id in record_ids
        )
        or len(set(record_ids)) != len(record_ids)
    ):
        raise ValueError("legacy frontier exclusions must be unique record ids")
    return _legacy_frontier_receipt(
        problem_id, excluded_record_ids=frozenset(record_ids)
    )


LEGACY_TOOL_NAMES = frozenset(
    {
        "search_matlas_theorems",
        "search_arxiv_theorems",
        "read_arxiv_primary",
        "append_route_terminal_report",
        "verify_blueprint_service",
        "memory_init",
        "memory_append",
        "memory_append_batch",
        "memory_search",
        "branch_update",
    }
)


def build_mcp_app() -> Optional[Any]:
    if FastMCP is None:
        return None
    app = FastMCP("reasoning-agent")

    def register(name: str):
        if name not in LEGACY_TOOL_NAMES:
            raise AssertionError("legacy MCP registration escaped its allowlist")

        def decorator(function: Any) -> Any:
            return app.tool(name=name)(function)

        return decorator

    @register("search_matlas_theorems")
    def tool_search_matlas_theorems(
        query: str, num_results: int = 10
    ) -> Dict[str, Any]:
        return search_matlas_theorems(query=query, num_results=num_results)

    @register("search_arxiv_theorems")
    def tool_search_arxiv_theorems(
        problem_id: str, query: str, num_results: int = 10
    ) -> Dict[str, Any]:
        return search_arxiv_theorems_for_problem(
            problem_id=problem_id,
            query=query,
            num_results=num_results,
        )

    @register("read_arxiv_primary")
    def tool_read_arxiv_primary(
        problem_id: str,
        arxiv_id: str,
        locator: str,
        max_excerpt_bytes: int = 20_000,
    ) -> Dict[str, Any]:
        return read_arxiv_primary_for_problem(
            problem_id=problem_id,
            arxiv_id=arxiv_id,
            locator=locator,
            max_excerpt_bytes=max_excerpt_bytes,
        )

    @register("append_route_terminal_report")
    def tool_append_route_terminal_report(
        problem_id: str,
        thread_id: str,
        plan_id: str,
        status: str,
        report_text: str,
        remaining_obligations: List[str],
        decisive_stuck_points: List[str],
    ) -> Any:
        try:
            receipt = append_route_terminal_report(
                problem_id=problem_id,
                thread_id=thread_id,
                plan_id=plan_id,
                status=status,
                report_text=report_text,
                remaining_obligations=remaining_obligations,
                decisive_stuck_points=decisive_stuck_points,
            )
        except MemoryCheckpointPreflightError as exc:
            return _checkpoint_failure_tool_result(exc)
        return _exact_checkpoint_tool_result(receipt)

    @register("verify_blueprint_service")
    def tool_verify_blueprint_service(problem_id: str) -> Dict[str, Any]:
        return verify_blueprint_service(problem_id=problem_id)

    @register("memory_init")
    def tool_memory_init(
        problem_id: str, meta: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        return memory_init(problem_id=problem_id, meta=meta)

    @register("memory_append")
    def tool_memory_append(
        problem_id: str,
        channel: str,
        record: Dict[str, Any],
        active: bool = True,
        supersedes: Optional[List[str]] = None,
        return_mode: str = "metadata",
    ) -> Dict[str, Any]:
        return memory_append(
            problem_id=problem_id,
            channel=channel,
            record=record,
            active=active,
            supersedes=supersedes,
            return_mode=return_mode,
        )

    @register("memory_append_batch")
    def tool_memory_append_batch(
        problem_id: str, items: List[Dict[str, Any]]
    ) -> Any:
        try:
            receipt = memory_append_batch(problem_id=problem_id, items=items)
        except MemoryCheckpointPreflightError as exc:
            return _checkpoint_failure_tool_result(exc)
        return _exact_checkpoint_tool_result(receipt)

    @register("memory_search")
    def tool_memory_search(
        problem_id: str,
        query: str,
        channels: Optional[List[str]] = None,
        limit_per_channel: int = 10,
        max_chars: int = DEFAULT_MEMORY_SEARCH_MAX_CHARS,
        include_inactive: bool = False,
        newest_first: bool = True,
    ) -> Dict[str, Any]:
        return memory_search(
            problem_id=problem_id,
            query=query,
            channels=channels,
            limit_per_channel=limit_per_channel,
            max_chars=max_chars,
            include_inactive=include_inactive,
            newest_first=newest_first,
        )

    @register("branch_update")
    def tool_branch_update(
        problem_id: str, branch_id: str, state: Dict[str, Any]
    ) -> Dict[str, Any]:
        return branch_update(problem_id=problem_id, branch_id=branch_id, state=state)

    return app


APP = build_mcp_app()


def main() -> None:
    control_token = os.environ.get("RETHLAS_GENERATION_CONTROL_TOKEN", "")
    if len(sys.argv) == 3 and sys.argv[1] == "--legacy-frontier-receipt":
        receipt = legacy_frontier_receipt(sys.argv[2])
        print(canonical_json_bytes(receipt).decode("utf-8"))
        return
    if len(sys.argv) == 3 and sys.argv[1] == "--generation-control-state":
        print(generation_control_status(sys.argv[2], control_token)["state"])
        return
    if len(sys.argv) == 3 and sys.argv[1] == "--generation-control-resume":
        generation_control_resume(sys.argv[2], control_token)
        return
    if len(sys.argv) == 3 and sys.argv[1] == "--generation-control-receipt":
        receipt = generation_control_receipt(sys.argv[2], control_token)
        print(canonical_json_bytes(receipt).decode("utf-8"))
        return
    if APP is None:
        raise SystemExit(
            "the official MCP SDK is missing or incompatible. Install "
            "requirements from mcp/requirements.txt first."
        )
    APP.run()


if __name__ == "__main__":
    main()
'''


def _defined_names(node: ast.AST) -> Iterable[str]:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        yield node.name
    elif isinstance(node, (ast.Assign, ast.AnnAssign)):
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for target in targets:
            if isinstance(target, ast.Name):
                yield target.id


def _loaded_names(node: ast.AST) -> set[str]:
    return {
        item.id
        for item in ast.walk(node)
        if isinstance(item, ast.Name) and isinstance(item.ctx, ast.Load)
    }


def _literal_truth(node: ast.AST) -> bool | None:
    if isinstance(node, ast.Constant):
        return bool(node.value)
    if isinstance(node, (ast.Dict, ast.List, ast.Set, ast.Tuple)):
        values = node.keys if isinstance(node, ast.Dict) else node.elts
        return bool(values)
    return None


class _LegacyConstantFolder(ast.NodeTransformer):
    """Specialize one copied function before dependency discovery."""

    def __init__(
        self,
        *,
        constants: dict[str, ast.expr] | None = None,
        drop_assignments: frozenset[str] = frozenset(),
        replacement_assignments: dict[str, ast.expr] | None = None,
    ) -> None:
        self.constants = constants or {}
        self.drop_assignments = drop_assignments
        self.replacement_assignments = replacement_assignments or {}

    def visit_Name(self, node: ast.Name) -> ast.AST:
        if isinstance(node.ctx, ast.Load) and node.id in self.constants:
            return ast.copy_location(self.constants[node.id], node)
        return node

    def visit_Assign(self, node: ast.Assign) -> ast.AST | None:
        if len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
            if name in self.drop_assignments:
                return None
            if name in self.replacement_assignments:
                node.value = self.replacement_assignments[name]
                return node
        return self.generic_visit(node)

    def visit_UnaryOp(self, node: ast.UnaryOp) -> ast.AST:
        node = self.generic_visit(node)
        if isinstance(node.op, ast.Not):
            truth = _literal_truth(node.operand)
            if truth is not None:
                return ast.copy_location(ast.Constant(not truth), node)
        return node

    def visit_BoolOp(self, node: ast.BoolOp) -> ast.AST:
        node = self.generic_visit(node)
        values: list[ast.expr] = []
        if isinstance(node.op, ast.And):
            for value in node.values:
                truth = _literal_truth(value)
                if truth is False:
                    return ast.copy_location(ast.Constant(False), node)
                if truth is not True:
                    values.append(value)
            identity = True
        else:
            for value in node.values:
                truth = _literal_truth(value)
                if truth is True:
                    return ast.copy_location(ast.Constant(True), node)
                if truth is not False:
                    values.append(value)
            identity = False
        if not values:
            return ast.copy_location(ast.Constant(identity), node)
        if len(values) == 1:
            return ast.copy_location(values[0], node)
        node.values = values
        return node

    def visit_IfExp(self, node: ast.IfExp) -> ast.AST:
        node = self.generic_visit(node)
        truth = _literal_truth(node.test)
        if truth is None:
            return node
        return ast.copy_location(node.body if truth else node.orelse, node)

    def visit_If(self, node: ast.If) -> ast.AST | list[ast.stmt]:
        node = self.generic_visit(node)
        truth = _literal_truth(node.test)
        if truth is None:
            return node
        return node.body if truth else node.orelse


def _insert_function_guard(node: ast.FunctionDef, source: str) -> None:
    guard = ast.parse(source).body
    insertion = 1 if (
        node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    ) else 0
    node.body[insertion:insertion] = guard


def _replace_wait_branch(node: ast.FunctionDef) -> None:
    replacement = ast.parse(
        'raise ValueError("legacy generation control forbids owner-wait states")'
    ).body
    for candidate in ast.walk(node):
        if not isinstance(candidate, ast.If):
            continue
        test = candidate.test
        if (
            isinstance(test, ast.Compare)
            and isinstance(test.left, ast.Name)
            and test.left.id == "state"
            and len(test.ops) == 1
            and isinstance(test.ops[0], ast.Eq)
            and len(test.comparators) == 1
            and isinstance(test.comparators[0], ast.Constant)
            and test.comparators[0].value == "running"
        ):
            candidate.orelse = replacement
            return
    raise RuntimeError(f"running-state branch disappeared from {node.name}")


def _specialize_node(node: ast.AST) -> ast.AST:
    if not isinstance(node, ast.FunctionDef):
        return node
    replacement = FUNCTION_REPLACEMENTS.get(node.name)
    if replacement is not None:
        parsed = ast.parse(replacement).body
        if len(parsed) != 1 or not isinstance(parsed[0], ast.FunctionDef):
            raise RuntimeError(f"invalid function replacement for {node.name}")
        return ast.fix_missing_locations(parsed[0])
    if node.name == "memory_append":
        _insert_function_guard(
            node,
            """
if channel == "failed_paths" and isinstance(record, dict) and (
    record.get("schema_version") == "rethlas_round_failure_synthesis_v1"
    or record.get("record_type") == "key_failures_summary"
):
    raise ValueError(
        "round failure synthesis requires one content-addressed "
        "memory_append_batch checkpoint"
    )
""",
        )
    elif node.name == "memory_append_batch":
        folder = _LegacyConstantFolder(
            constants={"released_registry": ast.Constant(False)},
            drop_assignments=frozenset({"released_registry", "publication_class"}),
            replacement_assignments={"initial_cutoffs": ast.Constant(None)},
        )
        node = folder.visit(node)
        if (
            not node.body
            or not isinstance(node.body[0], ast.Expr)
            or not isinstance(node.body[0].value, ast.Constant)
            or not isinstance(node.body[0].value.value, str)
        ):
            raise RuntimeError("memory_append_batch docstring disappeared")
        node.body[0].value.value = (
            "Append one bounded, content-addressed local Legacy checkpoint."
        )
        _insert_function_guard(
            node,
            """
if _trusted_control_publication or _trusted_publication_preflight is not None:
    raise ValueError("legacy MCP cannot publish host control memory")
items = _materialize_bound_external_plan_checkpoint_items(problem_id, items)
""",
        )
    elif node.name == "_publish_memory_batch_commit_once":
        folder = _LegacyConstantFolder(
            constants={
                "initial_cutoffs": ast.Constant(None),
                "publication_preflight": ast.Constant(None),
            },
            drop_assignments=frozenset({"refreshed_cutoffs"}),
            replacement_assignments={"cutoffs": ast.Constant(None)},
        )
        node = folder.visit(node)
        _insert_function_guard(
            node,
            """
if initial_cutoffs is not None or publication_preflight is not None:
    raise ValueError("legacy MCP cannot bind host publication cutoffs")
""",
        )
    elif node.name == "_iter_memory_batch_checkpoints":
        folder = _LegacyConstantFolder(
            constants={
                "released": ast.Constant(False),
                "registry_manifest": ast.Dict(keys=[], values=[]),
            },
            drop_assignments=frozenset({"released"}),
            replacement_assignments={
                "registry_manifest": ast.Dict(keys=[], values=[]),
            },
        )
        node = folder.visit(node)
        _insert_function_guard(
            node,
            """
if owner_manifest_snapshot_json is not None:
    raise ValueError("legacy MCP rejects owner publication snapshots")
""",
        )
    elif node.name == "_load_memory_entries":
        folder = _LegacyConstantFolder(
            constants={"released": ast.Constant(False)},
            drop_assignments=frozenset({"released"}),
        )
        node = folder.visit(node)
        _insert_function_guard(
            node,
            """
if owner_manifest_snapshot_json is not None:
    raise ValueError("legacy MCP rejects owner publication snapshots")
""",
        )
    elif node.name in {"_generation_control_payload", "generation_control_status"}:
        _replace_wait_branch(node)
        if node.name == "generation_control_status":
            _insert_function_guard(
                node,
                """
if owner_manifest_snapshot_json is not None:
    raise ValueError("legacy MCP rejects owner publication snapshots")
""",
            )
    return ast.fix_missing_locations(node)


def _audit_generated(generated: str) -> None:
    tree = ast.parse(generated, filename="legacy_server.py")
    forbidden_definitions = {
        "advisor_report_get",
        "continuous_round_finish",
        "continuous_round_status",
        "context_handoff_get",
        "context_handoff_prepare",
        "context_handoff_status",
        "generation_yield",
        "review_frontier_status",
        "route_cycle_close",
        "route_review_close",
        "route_review_prepare",
        "route_review_status",
        "route_review_wait",
        "verify_review_claim",
    }
    definitions = {
        item.name
        for item in ast.walk(tree)
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }
    leaked_definitions = sorted(definitions & forbidden_definitions)
    loaded = _loaded_names(tree)
    leaked_adapters = sorted(name for name in loaded if name.startswith("_adapter_"))
    leaked_blocked = sorted(loaded & BLOCKED_NAMES)
    forbidden_imports: list[str] = []
    for item in ast.walk(tree):
        if isinstance(item, ast.Import):
            names = [alias.name for alias in item.names]
        elif isinstance(item, ast.ImportFrom):
            names = [item.module or ""]
        else:
            continue
        forbidden_imports.extend(
            name
            for name in names
            if name.startswith(("review", "advisor_client", "review_client"))
        )
    failures = [
        *(f"definition:{name}" for name in leaked_definitions),
        *(f"adapter:{name}" for name in leaked_adapters),
        *(f"blocked:{name}" for name in leaked_blocked),
        *(f"import:{name}" for name in sorted(forbidden_imports)),
    ]
    if failures:
        raise RuntimeError("Legacy MCP isolation audit failed: " + ", ".join(failures))


def _audit_source_contract(
    source_text: str,
    node_by_name: dict[str, ast.AST],
) -> None:
    for name, expected in SOURCE_SIGNATURES.items():
        node = node_by_name.get(name)
        if not isinstance(node, ast.FunctionDef):
            raise RuntimeError(f"Legacy source function disappeared: {name}")
        actual = ast.unparse(node.args)
        if actual != expected:
            raise RuntimeError(
                f"Legacy source signature changed for {name}: {actual!r}"
            )

    # ``ast.dump`` is not a stable trust commitment across Python releases:
    # newly added AST fields change its bytes even when the source is
    # identical. Bind the exact source segment instead. This is deliberately
    # stricter than a normalized AST hash: formatting or comment changes in a
    # replaced function also require an explicit review and digest update.
    for name, expected in REPLACED_SOURCE_SHA256.items():
        node = node_by_name.get(name)
        if not isinstance(node, ast.FunctionDef):
            raise RuntimeError(f"Legacy replaced source function disappeared: {name}")
        source_segment = ast.get_source_segment(source_text, node)
        if source_segment is None:
            raise RuntimeError(
                f"Legacy replaced source function cannot be bound: {name}"
            )
        actual = hashlib.sha256(source_segment.encode("utf-8")).hexdigest()
        if actual != expected:
            raise RuntimeError(
                f"Legacy replacement requires review after source change: {name}"
            )

    channels_node = node_by_name.get("CHANNEL_FILES")
    if not isinstance(channels_node, ast.AnnAssign):
        raise RuntimeError("full MCP CHANNEL_FILES assignment disappeared")
    channels = ast.literal_eval(channels_node.value)
    if channels != EXPECTED_FULL_CHANNELS:
        raise RuntimeError("full MCP memory channel contract changed")


def generate(source_path: Path) -> str:
    source_text = source_path.read_text(encoding="utf-8")
    tree = ast.parse(source_text, filename=str(source_path))
    source_node_by_name: dict[str, ast.AST] = {}
    for node in tree.body:
        for name in _defined_names(node):
            source_node_by_name[name] = node
    _audit_source_contract(source_text, source_node_by_name)

    node_by_name: dict[str, ast.AST] = {}
    top_level_nodes: list[ast.AST] = []
    for raw_node in tree.body:
        node = _specialize_node(raw_node)
        names = list(_defined_names(node))
        if not names:
            continue
        top_level_nodes.append(node)
        for name in names:
            node_by_name[name] = node

    selected_names = set(ROOT_EXPORTS)
    selected_nodes: set[int] = set()
    pending = list(ROOT_EXPORTS)
    while pending:
        name = pending.pop()
        if name in PROVIDED_NAMES or name in BLOCKED_NAMES or name.startswith("_adapter_"):
            continue
        node = node_by_name.get(name)
        if node is None or id(node) in selected_nodes:
            continue
        selected_nodes.add(id(node))
        for dependency in _loaded_names(node):
            if dependency not in selected_names:
                selected_names.add(dependency)
                pending.append(dependency)

    missing_roots = sorted(ROOT_EXPORTS - set(node_by_name))
    if missing_roots:
        raise RuntimeError("legacy export roots disappeared: " + ", ".join(missing_roots))

    body = []
    for node in top_level_nodes:
        if id(node) not in selected_nodes:
            continue
        rendered = ast.unparse(node)
        body.append(rendered)
    generated = HEADER + "\n\n" + "\n\n".join(body) + FOOTER
    _audit_generated(generated)
    return generated


def generate_verification_client(source_path: Path) -> str:
    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    node_by_name: dict[str, ast.AST] = {}
    top_level_nodes: list[ast.AST] = []
    for node in tree.body:
        names = list(_defined_names(node))
        if not names:
            continue
        top_level_nodes.append(node)
        for name in names:
            node_by_name[name] = node

    root = node_by_name.get("verify_blueprint_file")
    if not isinstance(root, ast.FunctionDef):
        raise RuntimeError("whole-blueprint verification export disappeared")
    expected_signature = (
        "*, statement: str, draft_path: Path, verified_path: Path, endpoint: str, "
        "verification_deadline_utc: str | None=None, timeout_seconds: int=3600, "
        "api_token: str | None=None, receipt_path: Path | None=None, "
        "problem_id: str | None=None, blueprint_root: Path | None=None, "
        "publication_state_root: Path | None=None, "
        "verification_quorum: int=2, "
        "supersedes: list[dict[str, str]] | None=None, "
        "verification_profile: str | None=None, "
        "on_verifier_dispatch: Callable[[], None] | None=None, "
        "prepared_only: bool=False, "
        "publication_authority_intent_sha256: str | None=None, "
        "on_publication_admission_recovery: "
        "Callable[[Mapping[str, Any]], Mapping[str, Any]] | None=None, "
        "resume_dispatched: bool=False"
    )
    actual_signature = ast.unparse(root.args)
    if actual_signature != expected_signature:
        raise RuntimeError(
            "Legacy whole-blueprint verification signature changed: "
            + repr(actual_signature)
        )

    selected_names = set(VERIFICATION_ROOT_EXPORTS)
    selected_nodes: set[int] = set()
    pending = list(VERIFICATION_ROOT_EXPORTS)
    while pending:
        name = pending.pop()
        if name in VERIFICATION_PROVIDED_NAMES:
            continue
        node = node_by_name.get(name)
        if node is None or id(node) in selected_nodes:
            continue
        selected_nodes.add(id(node))
        for dependency in _loaded_names(node):
            if dependency not in selected_names:
                selected_names.add(dependency)
                pending.append(dependency)

    missing = sorted(VERIFICATION_ROOT_EXPORTS - set(node_by_name))
    if missing:
        raise RuntimeError(
            "Legacy verification export roots disappeared: " + ", ".join(missing)
        )
    body = [
        ast.unparse(node)
        for node in top_level_nodes
        if id(node) in selected_nodes
    ]
    generated = VERIFICATION_HEADER + "\n\n" + "\n\n".join(body) + "\n"
    generated_tree = ast.parse(
        generated,
        filename="legacy_verification_client.py",
    )
    symbols = symtable.symtable(
        generated,
        "legacy_verification_client.py",
        "exec",
    )
    module_bindings = {
        symbol.get_name()
        for symbol in symbols.get_symbols()
        if symbol.is_assigned() or symbol.is_imported() or symbol.is_namespace()
    }
    global_references: set[str] = set()

    def collect_global_references(table: symtable.SymbolTable) -> None:
        global_references.update(
            symbol.get_name()
            for symbol in table.get_symbols()
            if symbol.is_referenced() and symbol.is_global()
        )
        for child in table.get_children():
            collect_global_references(child)

    collect_global_references(symbols)
    unresolved_globals = sorted(
        global_references
        - module_bindings
        - set(dir(builtins))
        - {"__file__", "__name__", "__package__"}
    )
    if unresolved_globals:
        raise RuntimeError(
            "Legacy verification generated unresolved globals: "
            + ", ".join(unresolved_globals)
        )
    definitions = {
        item.name
        for item in ast.walk(generated_tree)
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }
    leaked = sorted(definitions & VERIFICATION_FORBIDDEN_DEFINITIONS)
    lowered = generated.casefold()
    forbidden_text = sorted(
        token
        for token in ("targeted_claim", "targeted verifier", "review_id", "route_id")
        if token in lowered
    )
    if leaked or forbidden_text:
        failures = [
            *(f"definition:{name}" for name in leaked),
            *(f"text:{token}" for token in forbidden_text),
        ]
        raise RuntimeError(
            "Legacy verification isolation audit failed: " + ", ".join(failures)
        )
    return generated


def audit_legacy_proof_context(source_path: Path) -> None:
    raw = source_path.read_text(encoding="utf-8")
    tree = ast.parse(raw, filename=str(source_path))
    forbidden_definitions = {
        "advisor_report_get",
        "continuous_round_finish",
        "continuous_round_status",
        "context_handoff_get",
        "context_handoff_prepare",
        "context_handoff_status",
        "generation_yield",
        "route_review_close",
        "route_review_prepare",
        "route_review_status",
        "route_review_wait",
        "validate_targeted_claim_receipt",
        "verify_review_claim",
        "verify_targeted_claim_service",
    }
    definitions = {
        item.name
        for item in ast.walk(tree)
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }
    leaked = sorted(definitions & forbidden_definitions)
    forbidden_imports = []
    for item in ast.walk(tree):
        if isinstance(item, ast.Import):
            modules = [alias.name for alias in item.names]
        elif isinstance(item, ast.ImportFrom):
            modules = [item.module or ""]
        else:
            continue
        forbidden_imports.extend(
            module
            for module in modules
            if module.startswith(("review", "advisor_client", "review_client"))
        )
    lowered = raw.casefold()
    forbidden_text = sorted(
        token
        for token in (
            "targeted_claim",
            "review_id",
            "route_review",
            "generation_yield",
            "continuous_round_finish",
            "continuous_round_status",
        )
        if token in lowered
    )
    if leaked or forbidden_imports or forbidden_text:
        failures = [
            *(f"definition:{name}" for name in leaked),
            *(f"import:{name}" for name in sorted(forbidden_imports)),
            *(f"text:{token}" for token in forbidden_text),
        ]
        raise RuntimeError(
            "Legacy proof-context isolation audit failed: " + ", ".join(failures)
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--verification-source", type=Path)
    parser.add_argument("--verification-output", type=Path)
    parser.add_argument("--proof-context-source", type=Path)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true")
    mode.add_argument("--check", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    source = (args.source or root / "server.py").resolve(strict=True)
    output = args.output or root / "legacy_server.py"
    generated = generate(source)
    verification_source = (
        args.verification_source or root / "verification_client.py"
    ).resolve(strict=True)
    proof_context_source = (
        args.proof_context_source or root / "proof_context.py"
    ).resolve(strict=True)
    audit_legacy_proof_context(proof_context_source)
    verification_output = (
        args.verification_output or root / "legacy_verification_client.py"
    )
    generated_verification = generate_verification_client(verification_source)
    if args.check:
        stale = []
        if not output.is_file() or output.read_text(encoding="utf-8") != generated:
            stale.append(str(output))
        if (
            not verification_output.is_file()
            or verification_output.read_text(encoding="utf-8")
            != generated_verification
        ):
            stale.append(str(verification_output))
        if stale:
            print(
                "generated Legacy runtime is stale: " + ", ".join(stale),
                file=sys.stderr,
            )
            return 1
        return 0
    output.write_text(generated, encoding="utf-8")
    verification_output.write_text(generated_verification, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
