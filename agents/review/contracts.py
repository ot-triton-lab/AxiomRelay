"""Fail-closed schemas for independent route review and context handoff.

Review is strategic criticism, not mathematical verification.  In particular,
neither a review report nor a derived effective verdict can publish a proof.
The only bridge to verification is a digest-bound, non-publishing ticket for
one exact load-bearing claim.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping


REVIEW_SNAPSHOT_SCHEMA = "rethlas_route_review_snapshot_v3"
CONTEXT_HANDOFF_SCHEMA = "rethlas_context_handoff_v3"
CONTINUOUS_CONTEXT_HANDOFF_SCHEMA = "rethlas_continuous_context_handoff_v1"
TARGETED_CLAIM_TICKET_SCHEMA = "rethlas_targeted_claim_ticket_v2"

MAX_REVIEW_SNAPSHOT_BYTES = 131_072
MAX_CONTEXT_HANDOFF_BYTES = 32_768
MAX_REVIEW_REPORT_BYTES = 32_768
MAX_CANONICAL_RECORD_BODY_BYTES = 16_384
MAX_FRONTIER_RECORDS = 64
MAX_PROGRESS_RECORDS = 32
MAX_BLUEPRINT_ITEMS = 128
MAX_FALLBACK_ROUTE_CANDIDATES = 1
MAX_HANDOFF_RECORD_IDS = 96
MAX_HANDOFF_OBLIGATIONS = 16
MAX_EVIDENCE_IDS = 32
MAX_TEXT_BYTES = 8_192
MAX_SHORT_TEXT_BYTES = 2_048
MAX_STATEMENT_TEXT_BYTES = 65_536
MAX_BLUEPRINT_TEXT_BYTES = 65_536

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
REVIEW_ID_RE = re.compile(r"^review_[0-9a-f]{32}$")
SNAPSHOT_ID_RE = re.compile(r"^snap_[0-9a-f]{64}$")
HANDOFF_ID_RE = re.compile(r"^handoff_[0-9a-f]{64}$")
TICKET_ID_RE = re.compile(r"^claim_[0-9a-f]{32}$")
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
PROBLEM_ID_RE = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9-])?"
    r"(?:/[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9-])?)*$"
)
RECORD_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
BATCH_ID_RE = re.compile(r"^batch_[0-9a-f]{64}$")

REVIEW_CYCLES = frozenset({"minute60", "minute120"})
REVIEW_ORDINAL = {"minute60": 1, "minute120": 2}
REVIEW_VERDICTS = frozenset({"green", "yellow", "red"})
PROGRESS_KINDS = frozenset(
    {"new_lemma", "counterexample_excluded", "uncertainty_reduction"}
)
PREMISE_TARGET_STATUSES = frozenset({"match", "mismatch", "unclear"})
UNCERTAINTY_STATUSES = frozenset({"reduced", "not_reduced", "unclear"})
OBSTRUCTION_STATUSES = frozenset(
    {"none", "known_obstruction", "counterexample_risk"}
)
EFFECTIVE_ALLOWED_ACTION = {
    "green": "continue_to_next_milestone",
    "yellow": "one_bounded_cycle_on_fatal_doubt",
    "red": "freeze_route",
}

_SNAPSHOT_KEYS = {
    "schema_version",
    "run_id",
    "problem_id",
    "cycle_id",
    "cycle",
    "review_ordinal",
    "due_at_utc",
    "root_thread_id",
    "root_turn_id",
    "root_terminal_sha256",
    "route_id",
    "active_route",
    "statement_sha256",
    "statement_text",
    "blueprint_sha256",
    "blueprint_text",
    "blueprint_items",
    "fallback_route_candidates",
    "frontier_records",
    "progress_records",
    "prior_official_review",
}
_BLUEPRINT_ITEM_KEYS = {"label", "item_id", "claim_sha256"}
_ACTIVE_ROUTE_KEYS = {
    "route_id",
    "core_bridge",
    "obligations",
    "commitment_record_id",
    "commitment_batch_id",
    "commitment_timestamp_utc",
    "commitment_sha256",
}
_FALLBACK_ROUTE_KEYS = {
    "route_id",
    "core_bridge",
    "obligations",
    "commitment_record_id",
    "commitment_batch_id",
    "commitment_timestamp_utc",
    "evidence_record_ids",
    "commitment_sha256",
}
_RECORD_KEYS = {
    "record_id",
    "kind",
    "body",
    "channel",
    "batch_id",
    "timestamp_utc",
}
_PRIOR_REVIEW_KEYS = {
    "record_id",
    "review_id",
    "cycle_id",
    "cycle",
    "review_ordinal",
    "snapshot_sha256",
    "timestamp_utc",
    "report",
    "decision",
    "content_sha256",
}
_REPORT_KEYS = {
    "review_id",
    "snapshot_sha256",
    "route_id",
    "answers",
    "verdict",
    "fatal_doubt",
    "freeze_reason",
    "load_bearing_claim",
}
_ANSWER_KEYS = {
    "core_bridge",
    "premise_target_fit",
    "uncertainty_change",
    "obstruction_risk",
    "next_milestone",
}
_CONFIRMED_PROGRESS_KEYS = {"record_id", "kind"}
_HANDOFF_KEYS = {
    "schema_version",
    "purpose",
    "run_id",
    "problem_id",
    "from_thread_epoch",
    "statement_sha256",
    "blueprint_sha256",
    "cadence",
    "active_route",
    "last_review",
    "new_record_ids",
    "yellow_streak",
    "route_frozen",
    "pending",
    "obligations",
    "next_action",
}
_CONTINUOUS_HANDOFF_KEYS = {
    "schema_version",
    "purpose",
    "run_id",
    "problem_id",
    "from_thread_epoch",
    "statement_sha256",
    "blueprint_sha256",
    "supervisor",
    "latest_cohort_decision",
    "parked_lanes",
    "active_route",
    "new_record_ids",
    "obligations",
    "next_action",
    "pending",
}


class ReviewContractError(ValueError):
    """A review artifact violated its bounded, exact contract."""


def _reject_constant(value: str) -> None:
    raise ReviewContractError(f"non-finite JSON constant {value} is forbidden")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ReviewContractError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def strict_json_loads(raw: str | bytes, *, label: str) -> Any:
    """Load strict UTF-8 JSON, rejecting duplicates and non-finite numbers."""

    try:
        text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
    except UnicodeDecodeError as exc:
        raise ReviewContractError(f"{label} is not valid UTF-8") from exc
    try:
        return json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise ReviewContractError(f"{label} is not strict JSON: {exc}") from exc


def canonical_json_bytes(value: Any) -> bytes:
    """Return the only byte representation used for content addresses."""

    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ReviewContractError("artifact is not canonical JSON data") from exc
    # Round-tripping also catches non-string mapping keys that json.dumps would
    # otherwise coerce and ambiguous user-defined containers.
    decoded = strict_json_loads(encoded, label="canonical artifact")
    if decoded != value:
        raise ReviewContractError("artifact does not round-trip as canonical JSON")
    return encoded


def _exact_object(value: Any, keys: set[str], *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        missing = sorted(keys - set(value)) if isinstance(value, dict) else sorted(keys)
        extra = sorted(set(value) - keys) if isinstance(value, dict) else []
        raise ReviewContractError(
            f"{label} must have exactly its schema keys; missing={missing}, extra={extra}"
        )
    return value


def _bounded_text(
    value: Any,
    *,
    label: str,
    max_bytes: int = MAX_TEXT_BYTES,
    allow_empty: bool = False,
) -> str:
    if not isinstance(value, str):
        raise ReviewContractError(f"{label} must be a string")
    if not allow_empty and not value.strip():
        raise ReviewContractError(f"{label} must be non-empty")
    if "\x00" in value or len(value.encode("utf-8")) > max_bytes:
        raise ReviewContractError(f"{label} exceeds its safe text bound")
    return value


def _bounded_obligations(value: Any, *, label: str) -> list[str]:
    if not isinstance(value, list) or not value or len(value) > MAX_HANDOFF_OBLIGATIONS:
        raise ReviewContractError(f"{label} must be a non-empty bounded array")
    normalized = [
        _bounded_text(
            item,
            label=f"{label}[{index}]",
            max_bytes=MAX_SHORT_TEXT_BYTES,
        )
        for index, item in enumerate(value)
    ]
    if len(set(normalized)) != len(normalized):
        raise ReviewContractError(f"{label} must be unique")
    return normalized


def _safe_id(value: Any, *, label: str, pattern: re.Pattern[str] = SAFE_ID_RE) -> str:
    text = _bounded_text(value, label=label, max_bytes=256)
    if pattern.fullmatch(text) is None:
        raise ReviewContractError(f"{label} has an invalid identifier shape")
    return text


def _sha256(value: Any, *, label: str, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or SHA256_RE.fullmatch(value) is None:
        raise ReviewContractError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _canonical_utc(value: Any, *, label: str) -> str:
    text = _bounded_text(value, label=label, max_bytes=64)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ReviewContractError(f"{label} must be canonical UTC") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ReviewContractError(f"{label} must be canonical UTC")
    if text != parsed.astimezone(timezone.utc).isoformat():
        raise ReviewContractError(f"{label} must be canonical UTC")
    return text


def _bounded_unique_ids(
    value: Any,
    *,
    label: str,
    maximum: int = MAX_EVIDENCE_IDS,
) -> list[str]:
    if not isinstance(value, list) or len(value) > maximum:
        raise ReviewContractError(f"{label} must be a bounded array")
    result: list[str] = []
    for index, item in enumerate(value):
        result.append(
            _safe_id(item, label=f"{label}[{index}]", pattern=RECORD_ID_RE)
        )
    if len(set(result)) != len(result):
        raise ReviewContractError(f"{label} must not contain duplicate ids")
    return result


def _validate_record_array(
    value: Any,
    *,
    label: str,
    maximum: int,
    progress: bool,
) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) > maximum:
        raise ReviewContractError(f"{label} must be a bounded array")
    normalized: list[dict[str, Any]] = []
    ids: list[str] = []
    for index, raw in enumerate(value):
        item = _exact_object(raw, _RECORD_KEYS, label=f"{label}[{index}]")
        record_id = _safe_id(
            item["record_id"],
            label=f"{label}[{index}].record_id",
            pattern=RECORD_ID_RE,
        )
        channel = _safe_id(item["channel"], label=f"{label}[{index}].channel")
        batch_id = _safe_id(
            item["batch_id"],
            label=f"{label}[{index}].batch_id",
            pattern=BATCH_ID_RE,
        )
        timestamp_utc = _canonical_utc(
            item["timestamp_utc"], label=f"{label}[{index}].timestamp_utc"
        )
        if not isinstance(item["body"], dict):
            raise ReviewContractError(f"{label}[{index}].body must be an object")
        durable_kind = item["body"].get("review_progress_kind", channel)
        kind = _safe_id(item["kind"], label=f"{label}[{index}].kind")
        if kind != durable_kind:
            raise ReviewContractError(
                f"{label}[{index}].kind disagrees with its durable body/channel"
            )
        if progress and kind not in PROGRESS_KINDS:
            raise ReviewContractError(
                f"{label}[{index}].kind is not qualifying progress"
            )
        body_bytes = canonical_json_bytes(item["body"])
        if len(body_bytes) > MAX_CANONICAL_RECORD_BODY_BYTES:
            raise ReviewContractError(f"{label}[{index}].body exceeds its bound")
        ids.append(record_id)
        normalized.append(
            {
                "record_id": record_id,
                "kind": kind,
                "body": deepcopy(item["body"]),
                "channel": channel,
                "batch_id": batch_id,
                "timestamp_utc": timestamp_utc,
            }
        )
    if len(set(ids)) != len(ids):
        raise ReviewContractError(f"{label} contains duplicate record ids")
    return normalized


def _validate_blueprint_items(
    value: Any, *, blueprint_sha256: str | None
) -> list[dict[str, str]]:
    if not isinstance(value, list) or len(value) > MAX_BLUEPRINT_ITEMS:
        raise ReviewContractError("review snapshot blueprint_items must be bounded")
    if blueprint_sha256 is None and value:
        raise ReviewContractError("review snapshot has blueprint items without a blueprint")
    normalized: list[dict[str, str]] = []
    labels: list[str] = []
    item_ids: list[str] = []
    for index, raw in enumerate(value):
        item = _exact_object(
            raw, _BLUEPRINT_ITEM_KEYS, label=f"review snapshot blueprint_items[{index}]"
        )
        label = _safe_id(
            item["label"], label=f"review snapshot blueprint_items[{index}].label"
        )
        item_id = _safe_id(
            item["item_id"],
            label=f"review snapshot blueprint_items[{index}].item_id",
            pattern=re.compile(r"^pi_[0-9a-f]{24}$"),
        )
        claim_sha256 = _sha256(
            item["claim_sha256"],
            label=f"review snapshot blueprint_items[{index}].claim_sha256",
        )
        labels.append(label)
        item_ids.append(item_id)
        normalized.append(
            {
                "label": label,
                "item_id": item_id,
                "claim_sha256": claim_sha256,
            }
        )
    if len(set(labels)) != len(labels):
        raise ReviewContractError("review snapshot blueprint item labels must be unique")
    if len(set(item_ids)) != len(item_ids):
        raise ReviewContractError("review snapshot blueprint item ids must be unique")
    return normalized


def _validate_fallback_route_candidates(
    value: Any, *, active_route_id: str, due_at: datetime
) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) > MAX_FALLBACK_ROUTE_CANDIDATES:
        raise ReviewContractError(
            "review snapshot fallback_route_candidates must contain at most one item"
        )
    normalized: list[dict[str, Any]] = []
    for index, raw in enumerate(value):
        item = _exact_object(
            raw,
            _FALLBACK_ROUTE_KEYS,
            label=f"fallback_route_candidates[{index}]",
        )
        route_id = _safe_id(item["route_id"], label="fallback route_id")
        if route_id == active_route_id:
            raise ReviewContractError("fallback route must differ from the active route")
        seed = {
            "route_id": route_id,
            "core_bridge": _bounded_text(
                item["core_bridge"], label="fallback core_bridge"
            ),
            "obligations": _bounded_obligations(
                item["obligations"], label="fallback obligations"
            ),
            "commitment_record_id": _safe_id(
                item["commitment_record_id"],
                label="fallback commitment_record_id",
                pattern=RECORD_ID_RE,
            ),
            "commitment_batch_id": _safe_id(
                item["commitment_batch_id"],
                label="fallback commitment_batch_id",
                pattern=BATCH_ID_RE,
            ),
            "commitment_timestamp_utc": _canonical_utc(
                item["commitment_timestamp_utc"],
                label="fallback commitment_timestamp_utc",
            ),
            "evidence_record_ids": _bounded_unique_ids(
                item["evidence_record_ids"],
                label="fallback evidence_record_ids",
            ),
        }
        if not seed["evidence_record_ids"]:
            raise ReviewContractError("fallback route requires durable evidence ids")
        if datetime.fromisoformat(seed["commitment_timestamp_utc"]) > due_at:
            raise ReviewContractError("fallback route was committed after the review due time")
        commitment_sha = _sha256(
            item["commitment_sha256"], label="fallback commitment_sha256"
        )
        if hashlib.sha256(canonical_json_bytes(seed)).hexdigest() != commitment_sha:
            raise ReviewContractError("fallback route commitment digest mismatch")
        normalized.append({**seed, "commitment_sha256": commitment_sha})
    return normalized


def _validate_active_route(
    value: Any, *, route_id: str, due_at: datetime
) -> dict[str, str]:
    item = _exact_object(value, _ACTIVE_ROUTE_KEYS, label="active_route")
    seed = {
        "route_id": _safe_id(item["route_id"], label="active route_id"),
        "core_bridge": _bounded_text(
            item["core_bridge"], label="active route core_bridge"
        ),
        "obligations": _bounded_obligations(
            item["obligations"], label="active route obligations"
        ),
        "commitment_record_id": _safe_id(
            item["commitment_record_id"],
            label="active route commitment_record_id",
            pattern=RECORD_ID_RE,
        ),
        "commitment_batch_id": _safe_id(
            item["commitment_batch_id"],
            label="active route commitment_batch_id",
            pattern=BATCH_ID_RE,
        ),
        "commitment_timestamp_utc": _canonical_utc(
            item["commitment_timestamp_utc"],
            label="active route commitment_timestamp_utc",
        ),
    }
    if seed["route_id"] != route_id:
        raise ReviewContractError(
            "active route commitment does not match snapshot route_id"
        )
    if datetime.fromisoformat(seed["commitment_timestamp_utc"]) > due_at:
        raise ReviewContractError(
            "active route was committed after the review due time"
        )
    commitment_sha = _sha256(
        item["commitment_sha256"], label="active route commitment_sha256"
    )
    if hashlib.sha256(canonical_json_bytes(seed)).hexdigest() != commitment_sha:
        raise ReviewContractError("active route commitment digest mismatch")
    return {**seed, "commitment_sha256": commitment_sha}


def _validate_prior_report(report: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the prior critic semantics without its retired full snapshot."""

    raw = _exact_object(report, _REPORT_KEYS, label="prior official review report")
    answers = _exact_object(
        raw["answers"], _ANSWER_KEYS, label="prior official review answers"
    )
    _bounded_text(answers["core_bridge"], label="prior review core_bridge")
    premise = _exact_object(
        answers["premise_target_fit"],
        {"status", "reason"},
        label="prior review premise_target_fit",
    )
    if premise["status"] not in PREMISE_TARGET_STATUSES:
        raise ReviewContractError("prior review premise_target_fit status is invalid")
    _bounded_text(premise["reason"], label="prior review premise reason")
    uncertainty = _exact_object(
        answers["uncertainty_change"],
        {"status", "evidence_ids", "confirmed_progress"},
        label="prior review uncertainty_change",
    )
    if uncertainty["status"] not in UNCERTAINTY_STATUSES:
        raise ReviewContractError("prior review uncertainty status is invalid")
    uncertainty_ids = _bounded_unique_ids(
        uncertainty["evidence_ids"], label="prior review uncertainty evidence"
    )
    if (uncertainty["status"] == "reduced") != bool(uncertainty_ids):
        raise ReviewContractError("prior review uncertainty evidence is inconsistent")
    confirmed_progress = uncertainty["confirmed_progress"]
    if not isinstance(confirmed_progress, list) or len(confirmed_progress) > MAX_PROGRESS_RECORDS:
        raise ReviewContractError("prior confirmed_progress must be a bounded array")
    confirmed_ids: list[str] = []
    for index, item in enumerate(confirmed_progress):
        entry = _exact_object(
            item,
            _CONFIRMED_PROGRESS_KEYS,
            label=f"prior confirmed_progress[{index}]",
        )
        confirmed_ids.append(
            _safe_id(
                entry["record_id"],
                label=f"prior confirmed_progress[{index}].record_id",
                pattern=RECORD_ID_RE,
            )
        )
        if entry["kind"] not in PROGRESS_KINDS:
            raise ReviewContractError("prior confirmed progress kind is invalid")
    if len(set(confirmed_ids)) != len(confirmed_ids):
        raise ReviewContractError("prior confirmed progress ids must be unique")
    obstruction = _exact_object(
        answers["obstruction_risk"],
        {"status", "detail", "evidence_ids"},
        label="prior review obstruction_risk",
    )
    if obstruction["status"] not in OBSTRUCTION_STATUSES:
        raise ReviewContractError("prior review obstruction status is invalid")
    _bounded_text(
        obstruction["detail"],
        label="prior review obstruction detail",
        allow_empty=obstruction["status"] == "none",
    )
    _bounded_unique_ids(
        obstruction["evidence_ids"], label="prior review obstruction evidence"
    )

    milestone = answers["next_milestone"]
    if milestone is not None:
        milestone = _exact_object(
            milestone,
            {"description", "test"},
            label="prior review next_milestone",
        )
        _bounded_text(milestone["description"], label="prior milestone description")
        _bounded_text(milestone["test"], label="prior milestone test")
    doubt = raw["fatal_doubt"]
    if doubt is not None:
        doubt = _exact_object(
            doubt, {"description", "test"}, label="prior review fatal_doubt"
        )
        _bounded_text(doubt["description"], label="prior doubt description")
        _bounded_text(doubt["test"], label="prior doubt test")
    freeze_reason = raw["freeze_reason"]
    if freeze_reason is not None:
        _bounded_text(freeze_reason, label="prior review freeze_reason")
    verdict = raw["verdict"]
    if verdict == "green":
        if milestone is None or doubt is not None or freeze_reason is not None:
            raise ReviewContractError("prior green review semantics are invalid")
    elif verdict == "yellow":
        if milestone is None or doubt is None or milestone != doubt or freeze_reason is not None:
            raise ReviewContractError("prior yellow review semantics are invalid")
    elif verdict == "red":
        if milestone is not None or doubt is not None or freeze_reason is None:
            raise ReviewContractError("prior red review semantics are invalid")
    else:
        raise ReviewContractError("prior review verdict is invalid")
    claim = raw["load_bearing_claim"]
    if claim is not None:
        claim = _exact_object(
            claim,
            {"blueprint_item_label", "claim_sha256", "reason"},
            label="prior review load_bearing_claim",
        )
        _safe_id(claim["blueprint_item_label"], label="prior claim label")
        _sha256(claim["claim_sha256"], label="prior claim digest")
        _bounded_text(claim["reason"], label="prior claim reason")
    if verdict == "red" and claim is not None:
        raise ReviewContractError(
            "prior red review verdict forbids a load-bearing verification claim"
        )
    if len(canonical_json_bytes(raw)) > MAX_REVIEW_REPORT_BYTES:
        raise ReviewContractError("prior review report exceeds its byte bound")
    return deepcopy(raw)


def validate_review_snapshot(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and copy one immutable reviewer input snapshot."""

    raw = _exact_object(snapshot, _SNAPSHOT_KEYS, label="review snapshot")
    if raw["schema_version"] != REVIEW_SNAPSHOT_SCHEMA:
        raise ReviewContractError("review snapshot schema_version is unsupported")
    run_id = _safe_id(raw["run_id"], label="review snapshot run_id")
    problem_id = _safe_id(
        raw["problem_id"], label="review snapshot problem_id", pattern=PROBLEM_ID_RE
    )
    cycle_id = _safe_id(raw["cycle_id"], label="review snapshot cycle_id")
    cycle = raw["cycle"]
    if cycle not in REVIEW_CYCLES:
        raise ReviewContractError("review snapshot cycle must be minute60 or minute120")
    ordinal = raw["review_ordinal"]
    if type(ordinal) is not int or ordinal != REVIEW_ORDINAL[cycle]:
        raise ReviewContractError("review ordinal does not match its cadence cycle")
    due_at_utc = _canonical_utc(
        raw["due_at_utc"], label="review snapshot due_at_utc"
    )
    root_thread_id = _safe_id(
        raw["root_thread_id"], label="review snapshot root_thread_id"
    )
    root_turn_id = _safe_id(raw["root_turn_id"], label="review snapshot root_turn_id")
    root_terminal_sha256 = _sha256(
        raw["root_terminal_sha256"],
        label="review snapshot root_terminal_sha256",
    )
    route_id = _safe_id(raw["route_id"], label="review snapshot route_id")
    due_at = datetime.fromisoformat(due_at_utc)
    active_route = _validate_active_route(
        raw["active_route"], route_id=route_id, due_at=due_at
    )
    statement_digest = _sha256(
        raw["statement_sha256"], label="review snapshot statement_sha256"
    )
    statement_text = _bounded_text(
        raw["statement_text"],
        label="review snapshot statement_text",
        max_bytes=MAX_STATEMENT_TEXT_BYTES,
    )
    if hashlib.sha256(statement_text.encode("utf-8")).hexdigest() != statement_digest:
        raise ReviewContractError("review snapshot statement body/digest mismatch")
    blueprint_digest = _sha256(
        raw["blueprint_sha256"],
        label="review snapshot blueprint_sha256",
        nullable=True,
    )
    blueprint_text_raw = raw["blueprint_text"]
    if blueprint_digest is None:
        if blueprint_text_raw is not None:
            raise ReviewContractError("review snapshot has blueprint text without a digest")
        blueprint_text = None
    else:
        blueprint_text = _bounded_text(
            blueprint_text_raw,
            label="review snapshot blueprint_text",
            max_bytes=MAX_BLUEPRINT_TEXT_BYTES,
            allow_empty=True,
        )
        if hashlib.sha256(blueprint_text.encode("utf-8")).hexdigest() != blueprint_digest:
            raise ReviewContractError("review snapshot blueprint body/digest mismatch")
    blueprint_items = _validate_blueprint_items(
        raw["blueprint_items"], blueprint_sha256=blueprint_digest
    )
    frontier = _validate_record_array(
        raw["frontier_records"],
        label="review snapshot frontier_records",
        maximum=MAX_FRONTIER_RECORDS,
        progress=False,
    )
    progress_records = _validate_record_array(
        raw["progress_records"],
        label="review snapshot progress_records",
        maximum=MAX_PROGRESS_RECORDS,
        progress=True,
    )
    frontier_ids = {record["record_id"] for record in frontier}
    progress_ids = {record["record_id"] for record in progress_records}
    if not progress_ids <= frontier_ids:
        raise ReviewContractError(
            "every progress record must also appear canonically in frontier_records"
        )
    frontier_by_id = {record["record_id"]: record for record in frontier}
    fallback_route_candidates = _validate_fallback_route_candidates(
        raw["fallback_route_candidates"],
        active_route_id=route_id,
        due_at=due_at,
    )
    fallback_evidence_ids = {
        record_id
        for candidate in fallback_route_candidates
        for record_id in candidate["evidence_record_ids"]
    }
    if not fallback_evidence_ids <= frontier_ids:
        raise ReviewContractError(
            "fallback route evidence must be present in the bound frontier"
        )
    for record in progress_records:
        frontier_record = frontier_by_id[record["record_id"]]
        if frontier_record != record:
            raise ReviewContractError(
                "progress and frontier durable records disagree for the same id"
            )
    if any(
        datetime.fromisoformat(record["timestamp_utc"]) > due_at
        for record in frontier
    ):
        raise ReviewContractError(
            "review snapshot cannot cite durable work created after its exact due time"
        )
    prior_raw = raw["prior_official_review"]
    prior_official_review: dict[str, Any] | None
    if prior_raw is None:
        if cycle == "minute120":
            raise ReviewContractError(
                "minute120 requires its same-cycle official minute60 review"
            )
        prior_official_review = None
    else:
        prior = _exact_object(
            prior_raw, _PRIOR_REVIEW_KEYS, label="prior official review"
        )
        prior_report = _validate_prior_report(prior["report"])
        decision_keys = {
            "route_id",
            "raw_verdict",
            "effective_verdict",
            "yellow_streak",
            "critic_confirmed_progress_ids",
            "auto_red",
            "auto_red_reason",
            "route_frozen",
            "allowed_action",
        }
        prior_decision = _exact_object(
            prior["decision"], decision_keys, label="prior official review decision"
        )
        if (
            prior_decision["raw_verdict"] not in REVIEW_VERDICTS
            or prior_decision["effective_verdict"] not in REVIEW_VERDICTS
            or type(prior_decision["yellow_streak"]) is not int
            or not 0 <= prior_decision["yellow_streak"] <= 2
            or type(prior_decision["auto_red"]) is not bool
            or type(prior_decision["route_frozen"]) is not bool
            or prior_decision["route_frozen"]
            != (prior_decision["effective_verdict"] == "red")
            or prior_decision["allowed_action"]
            != EFFECTIVE_ALLOWED_ACTION[prior_decision["effective_verdict"]]
        ):
            raise ReviewContractError("prior official review decision is inconsistent")
        prior_progress = _bounded_unique_ids(
            prior_decision["critic_confirmed_progress_ids"],
            label="prior official review progress ids",
        )
        prior_reason = prior_decision["auto_red_reason"]
        if prior_reason is not None:
            prior_reason = _bounded_text(
                prior_reason, label="prior official review auto_red_reason"
            )
        if prior_decision["auto_red"] != (prior_reason is not None):
            raise ReviewContractError("prior official review auto-red fields disagree")
        prior_route = _safe_id(
            prior_decision["route_id"], label="prior official review route_id"
        )
        if (
            prior_report.get("review_id") != prior["review_id"]
            or prior_report.get("snapshot_sha256") != prior["snapshot_sha256"]
            or prior_report.get("route_id") != prior_route
            or prior_report.get("verdict") != prior_decision["raw_verdict"]
        ):
            raise ReviewContractError("prior official report/decision bindings disagree")
        prior_report_progress = sorted(
            entry["record_id"]
            for entry in prior_report["answers"]["uncertainty_change"][
                "confirmed_progress"
            ]
        )
        if sorted(prior_progress) != prior_report_progress:
            raise ReviewContractError(
                "prior official confirmed progress report/decision disagree"
            )
        normalized_prior_decision = {
            **deepcopy(prior_decision),
            "route_id": prior_route,
            "critic_confirmed_progress_ids": prior_progress,
            "auto_red_reason": prior_reason,
        }
        prior_official_review = {
            "record_id": _safe_id(
                prior["record_id"],
                label="prior official review record_id",
                pattern=RECORD_ID_RE,
            ),
            "review_id": _safe_id(
                prior["review_id"],
                label="prior official review review_id",
                pattern=REVIEW_ID_RE,
            ),
            "cycle_id": _safe_id(
                prior["cycle_id"], label="prior official review cycle_id"
            ),
            "cycle": prior["cycle"],
            "review_ordinal": prior["review_ordinal"],
            "snapshot_sha256": _sha256(
                prior["snapshot_sha256"],
                label="prior official review snapshot_sha256",
            ),
            "timestamp_utc": _canonical_utc(
                prior["timestamp_utc"],
                label="prior official review timestamp_utc",
            ),
            "report": deepcopy(prior_report),
            "decision": normalized_prior_decision,
            "content_sha256": _sha256(
                prior["content_sha256"],
                label="prior official review content_sha256",
            ),
        }
        if (
            prior_official_review["cycle"] not in REVIEW_CYCLES
            or type(prior_official_review["review_ordinal"]) is not int
            or prior_official_review["review_ordinal"]
            != REVIEW_ORDINAL[prior_official_review["cycle"]]
        ):
            raise ReviewContractError(
                "prior official review cycle/ordinal binding is invalid"
            )
        if cycle == "minute120":
            if (
                prior_official_review["cycle_id"] != cycle_id
                or prior_official_review["cycle"] != "minute60"
                or prior_official_review["review_ordinal"] != 1
            ):
                raise ReviewContractError(
                    "minute120 requires its same-cycle official minute60 review"
                )
        elif (
            prior_official_review["cycle_id"] == cycle_id
            or prior_official_review["cycle"] != "minute120"
            or prior_official_review["review_ordinal"] != 2
        ):
            raise ReviewContractError(
                "minute60 prior review must be a prior-cycle official minute120 review"
            )
        if prior_route != route_id:
            raise ReviewContractError(
                "prior official review must belong to the same active route"
            )
        prior_seed = dict(prior_official_review)
        prior_seed.pop("content_sha256")
        if (
            hashlib.sha256(canonical_json_bytes(prior_seed)).hexdigest()
            != prior_official_review["content_sha256"]
        ):
            raise ReviewContractError("prior official review content digest mismatch")
        cutoff = datetime.fromisoformat(prior_official_review["timestamp_utc"])
        if cutoff > due_at:
            raise ReviewContractError(
                "prior official review cannot be newer than the current exact due time"
            )
        if any(
            datetime.fromisoformat(record["timestamp_utc"]) <= cutoff
            for record in progress_records
        ):
            raise ReviewContractError(
                "minute120 progress must be durably newer than the prior official review"
            )
    normalized = {
        "schema_version": REVIEW_SNAPSHOT_SCHEMA,
        "run_id": run_id,
        "problem_id": problem_id,
        "cycle_id": cycle_id,
        "cycle": cycle,
        "review_ordinal": ordinal,
        "due_at_utc": due_at_utc,
        "root_thread_id": root_thread_id,
        "root_turn_id": root_turn_id,
        "root_terminal_sha256": root_terminal_sha256,
        "route_id": route_id,
        "active_route": active_route,
        "statement_sha256": statement_digest,
        "statement_text": statement_text,
        "blueprint_sha256": blueprint_digest,
        "blueprint_text": blueprint_text,
        "blueprint_items": blueprint_items,
        "fallback_route_candidates": fallback_route_candidates,
        "frontier_records": frontier,
        "progress_records": progress_records,
        "prior_official_review": prior_official_review,
    }
    if len(canonical_json_bytes(normalized)) > MAX_REVIEW_SNAPSHOT_BYTES:
        raise ReviewContractError("review snapshot exceeds its total byte bound")
    return normalized


def snapshot_sha256(snapshot: Mapping[str, Any]) -> str:
    normalized = validate_review_snapshot(snapshot)
    return hashlib.sha256(canonical_json_bytes(normalized)).hexdigest()


def snapshot_id(snapshot: Mapping[str, Any]) -> str:
    return f"snap_{snapshot_sha256(snapshot)}"


def _evidence_ids(
    value: Any,
    *,
    label: str,
    snapshot_record_ids: set[str],
) -> list[str]:
    result = _bounded_unique_ids(value, label=label)
    if not set(result) <= snapshot_record_ids:
        raise ReviewContractError(f"{label} cites an id outside the bound snapshot")
    return result


def validate_review_report(
    report: Mapping[str, Any],
    *,
    review_id: str,
    snapshot: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate a critic report against its exact request and snapshot."""

    normalized_snapshot = validate_review_snapshot(snapshot)
    expected_snapshot_sha = snapshot_sha256(normalized_snapshot)
    raw = _exact_object(report, _REPORT_KEYS, label="route review report")
    expected_review_id = _safe_id(
        review_id, label="expected review_id", pattern=REVIEW_ID_RE
    )
    if raw["review_id"] != expected_review_id:
        raise ReviewContractError("route review report review_id binding mismatch")
    if raw["snapshot_sha256"] != expected_snapshot_sha:
        raise ReviewContractError("route review report snapshot binding mismatch")
    if raw["route_id"] != normalized_snapshot["route_id"]:
        raise ReviewContractError("route review report route binding mismatch")
    answers = _exact_object(raw["answers"], _ANSWER_KEYS, label="review answers")
    core_bridge = _bounded_text(
        answers["core_bridge"], label="review answers.core_bridge"
    )
    if core_bridge != normalized_snapshot["active_route"]["core_bridge"]:
        raise ReviewContractError(
            "review core bridge must copy the bound active-route commitment exactly"
        )
    premise_fit = _exact_object(
        answers["premise_target_fit"],
        {"status", "reason"},
        label="review answers.premise_target_fit",
    )
    if premise_fit["status"] not in PREMISE_TARGET_STATUSES:
        raise ReviewContractError("premise_target_fit.status is invalid")
    normalized_premise_fit = {
        "status": premise_fit["status"],
        "reason": _bounded_text(
            premise_fit["reason"], label="premise_target_fit.reason"
        ),
    }
    snapshot_record_ids = {
        record["record_id"] for record in normalized_snapshot["frontier_records"]
    }
    uncertainty = _exact_object(
        answers["uncertainty_change"],
        {"status", "evidence_ids", "confirmed_progress"},
        label="review answers.uncertainty_change",
    )
    if uncertainty["status"] not in UNCERTAINTY_STATUSES:
        raise ReviewContractError("uncertainty_change.status is invalid")
    uncertainty_ids = _evidence_ids(
        uncertainty["evidence_ids"],
        label="uncertainty_change.evidence_ids",
        snapshot_record_ids=snapshot_record_ids,
    )
    if uncertainty["status"] == "reduced" and not uncertainty_ids:
        raise ReviewContractError(
            "reduced uncertainty requires at least one bound evidence id"
        )
    if uncertainty["status"] != "reduced" and uncertainty_ids:
        raise ReviewContractError(
            "non-reduced uncertainty cannot claim progress evidence ids"
        )
    progress_by_id = {
        record["record_id"]: record for record in normalized_snapshot["progress_records"]
    }
    confirmed_raw = uncertainty["confirmed_progress"]
    if not isinstance(confirmed_raw, list) or len(confirmed_raw) > MAX_PROGRESS_RECORDS:
        raise ReviewContractError("confirmed_progress must be a bounded array")
    confirmed_progress: list[dict[str, str]] = []
    confirmed_ids: list[str] = []
    for index, item in enumerate(confirmed_raw):
        entry = _exact_object(
            item,
            _CONFIRMED_PROGRESS_KEYS,
            label=f"confirmed_progress[{index}]",
        )
        record_id = _safe_id(
            entry["record_id"],
            label=f"confirmed_progress[{index}].record_id",
            pattern=RECORD_ID_RE,
        )
        kind = entry["kind"]
        if kind not in PROGRESS_KINDS:
            raise ReviewContractError("confirmed progress kind is invalid")
        durable = progress_by_id.get(record_id)
        if durable is None:
            raise ReviewContractError(
                "confirmed progress id is outside the trusted progress records"
            )
        if durable["kind"] != kind:
            raise ReviewContractError(
                "confirmed progress kind disagrees with durable provenance"
            )
        confirmed_ids.append(record_id)
        confirmed_progress.append({"record_id": record_id, "kind": kind})
    if len(set(confirmed_ids)) != len(confirmed_ids):
        raise ReviewContractError("confirmed progress ids must be unique")
    obstruction = _exact_object(
        answers["obstruction_risk"],
        {"status", "detail", "evidence_ids"},
        label="review answers.obstruction_risk",
    )
    if obstruction["status"] not in OBSTRUCTION_STATUSES:
        raise ReviewContractError("obstruction_risk.status is invalid")
    obstruction_ids = _evidence_ids(
        obstruction["evidence_ids"],
        label="obstruction_risk.evidence_ids",
        snapshot_record_ids=snapshot_record_ids,
    )
    detail = _bounded_text(
        obstruction["detail"],
        label="obstruction_risk.detail",
        allow_empty=obstruction["status"] == "none",
    )
    milestone_raw = answers["next_milestone"]
    milestone: dict[str, str] | None
    if milestone_raw is None:
        milestone = None
    else:
        milestone_obj = _exact_object(
            milestone_raw,
            {"description", "test"},
            label="review answers.next_milestone",
        )
        milestone = {
            "description": _bounded_text(
                milestone_obj["description"], label="next_milestone.description"
            ),
            "test": _bounded_text(
                milestone_obj["test"], label="next_milestone.test"
            ),
        }
    verdict = raw["verdict"]
    if verdict not in REVIEW_VERDICTS:
        raise ReviewContractError("review verdict must be green, yellow, or red")
    doubt_raw = raw["fatal_doubt"]
    fatal_doubt: dict[str, str] | None
    if doubt_raw is None:
        fatal_doubt = None
    else:
        doubt_obj = _exact_object(
            doubt_raw, {"description", "test"}, label="review fatal_doubt"
        )
        fatal_doubt = {
            "description": _bounded_text(
                doubt_obj["description"], label="fatal_doubt.description"
            ),
            "test": _bounded_text(doubt_obj["test"], label="fatal_doubt.test"),
        }
    freeze_reason_raw = raw["freeze_reason"]
    freeze_reason = (
        None
        if freeze_reason_raw is None
        else _bounded_text(freeze_reason_raw, label="review freeze_reason")
    )
    claim_raw = raw["load_bearing_claim"]
    load_bearing_claim: dict[str, str] | None
    if claim_raw is None:
        load_bearing_claim = None
    else:
        claim_obj = _exact_object(
            claim_raw,
            {"blueprint_item_label", "claim_sha256", "reason"},
            label="review load_bearing_claim",
        )
        load_bearing_claim = {
            "blueprint_item_label": _safe_id(
                claim_obj["blueprint_item_label"],
                label="load_bearing_claim.blueprint_item_label",
            ),
            "claim_sha256": _sha256(
                claim_obj["claim_sha256"], label="load_bearing_claim.claim_sha256"
            ),
            "reason": _bounded_text(
                claim_obj["reason"], label="load_bearing_claim.reason"
            ),
        }
        if normalized_snapshot["blueprint_sha256"] is None:
            raise ReviewContractError(
                "load-bearing claim cannot be requested without a bound blueprint"
            )
        item_matches = [
            item
            for item in normalized_snapshot["blueprint_items"]
            if item["label"] == load_bearing_claim["blueprint_item_label"]
        ]
        if len(item_matches) != 1:
            raise ReviewContractError(
                "load-bearing claim label is not one unique bound blueprint item"
            )
        if item_matches[0]["claim_sha256"] != load_bearing_claim["claim_sha256"]:
            raise ReviewContractError(
                "load-bearing claim digest disagrees with its bound blueprint item"
            )

    # Red is already a terminal route judgment.  Asking a paid verifier to
    # adjudicate one of that route's claims would both delay the mandatory
    # freeze and give a red route an illicit path back into continuation.
    if verdict == "red" and load_bearing_claim is not None:
        raise ReviewContractError(
            "red review verdict forbids a load-bearing verification claim"
        )

    if verdict == "green":
        if milestone is None or fatal_doubt is not None or freeze_reason is not None:
            raise ReviewContractError(
                "green requires one next milestone and forbids doubt/freeze reason"
            )
    elif verdict == "yellow":
        if milestone is None or fatal_doubt is None or freeze_reason is not None:
            raise ReviewContractError(
                "yellow requires exactly one fatal doubt, its test, and a milestone"
            )
        if milestone != fatal_doubt:
            raise ReviewContractError(
                "yellow next milestone must be exactly the fatal-doubt test"
            )
    elif milestone is not None or fatal_doubt is not None or freeze_reason is None:
        raise ReviewContractError(
            "red requires a freeze reason and forbids milestone/fatal_doubt"
        )

    normalized = {
        "review_id": expected_review_id,
        "snapshot_sha256": expected_snapshot_sha,
        "route_id": normalized_snapshot["route_id"],
        "answers": {
            "core_bridge": core_bridge,
            "premise_target_fit": normalized_premise_fit,
            "uncertainty_change": {
                "status": uncertainty["status"],
                "evidence_ids": uncertainty_ids,
                "confirmed_progress": confirmed_progress,
            },
            "obstruction_risk": {
                "status": obstruction["status"],
                "detail": detail,
                "evidence_ids": obstruction_ids,
            },
            "next_milestone": milestone,
        },
        "verdict": verdict,
        "fatal_doubt": fatal_doubt,
        "freeze_reason": freeze_reason,
        "load_bearing_claim": load_bearing_claim,
    }
    if len(canonical_json_bytes(normalized)) > MAX_REVIEW_REPORT_BYTES:
        raise ReviewContractError("route review report exceeds its byte bound")
    return normalized


def _critic_confirmed_progress_ids(
    report: Mapping[str, Any], snapshot: Mapping[str, Any]
) -> list[str]:
    return sorted(
        entry["record_id"]
        for entry in report["answers"]["uncertainty_change"]["confirmed_progress"]
    )


def apply_effective_verdict(
    report: Mapping[str, Any],
    *,
    review_id: str,
    snapshot: Mapping[str, Any],
    previous_decision: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Derive host policy state, including the two-yellow automatic red rule.

    ``previous_decision`` is the durable prior decision for the immediately
    preceding official review.  A route switch or critic-confirmed qualifying
    progress resets the yellow streak.  The reviewer never chooses
    ``effective_verdict`` itself.
    """

    normalized_snapshot = validate_review_snapshot(snapshot)
    normalized_report = validate_review_report(
        report, review_id=review_id, snapshot=normalized_snapshot
    )
    confirmed_progress_ids = _critic_confirmed_progress_ids(
        normalized_report, normalized_snapshot
    )
    raw_verdict = normalized_report["verdict"]
    previous_same_route_yellow = False
    previous_same_route_frozen = False
    previous_streak = 0
    if previous_decision is not None:
        required = {
            "route_id",
            "effective_verdict",
            "yellow_streak",
            "route_frozen",
        }
        previous = _exact_object(
            previous_decision, required, label="previous review decision"
        )
        if (
            previous["route_id"] == normalized_snapshot["route_id"]
            and previous["effective_verdict"] == "yellow"
            and previous["route_frozen"] is False
        ):
            previous_same_route_yellow = True
            if type(previous["yellow_streak"]) is not int or previous["yellow_streak"] < 1:
                raise ReviewContractError("previous yellow_streak is invalid")
            previous_streak = previous["yellow_streak"]
        if (
            previous["route_id"] == normalized_snapshot["route_id"]
            and previous["effective_verdict"] == "red"
            and previous["route_frozen"] is True
        ):
            previous_same_route_frozen = True
    yellow_streak = 0
    effective = raw_verdict
    auto_red = False
    auto_red_reason: str | None = None
    if previous_same_route_frozen:
        effective = "red"
        auto_red = True
        auto_red_reason = "the same route was already frozen by an effective red review"
    elif raw_verdict == "yellow":
        if previous_same_route_yellow and not confirmed_progress_ids:
            yellow_streak = previous_streak + 1
            effective = "red"
            auto_red = True
            auto_red_reason = (
                "two consecutive same-route yellow reviews without a critic-confirmed "
                "new lemma, counterexample exclusion, or uncertainty reduction"
            )
        else:
            yellow_streak = 1
    route_frozen = effective == "red"
    return {
        "route_id": normalized_snapshot["route_id"],
        "raw_verdict": raw_verdict,
        "effective_verdict": effective,
        "yellow_streak": yellow_streak,
        "critic_confirmed_progress_ids": confirmed_progress_ids,
        "auto_red": auto_red,
        "auto_red_reason": auto_red_reason,
        "route_frozen": route_frozen,
        "allowed_action": EFFECTIVE_ALLOWED_ACTION[effective],
    }


def build_targeted_verification_ticket(
    report: Mapping[str, Any],
    *,
    review_id: str,
    snapshot: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Create the sole legal review-to-verifier bridge for one exact claim."""

    normalized_snapshot = validate_review_snapshot(snapshot)
    normalized_report = validate_review_report(
        report, review_id=review_id, snapshot=normalized_snapshot
    )
    claim = normalized_report["load_bearing_claim"]
    if claim is None:
        return None
    seed = {
        "review_id": normalized_report["review_id"],
        "snapshot_sha256": normalized_report["snapshot_sha256"],
        "route_id": normalized_report["route_id"],
        "blueprint_sha256": normalized_snapshot["blueprint_sha256"],
        "blueprint_item_id": next(
            item["item_id"]
            for item in normalized_snapshot["blueprint_items"]
            if item["label"] == claim["blueprint_item_label"]
        ),
        "claim": claim,
    }
    ticket_id = "claim_" + hashlib.sha256(canonical_json_bytes(seed)).hexdigest()[:32]
    return {
        "schema_version": TARGETED_CLAIM_TICKET_SCHEMA,
        "ticket_id": ticket_id,
        **seed,
        "verification_mode": "targeted_nonpublishing",
        "publication_authority": False,
        "whole_blueprint_verdict_authority": False,
    }


def validate_targeted_verification_ticket(ticket: Mapping[str, Any]) -> dict[str, Any]:
    keys = {
        "schema_version",
        "ticket_id",
        "review_id",
        "snapshot_sha256",
        "route_id",
        "blueprint_sha256",
        "blueprint_item_id",
        "claim",
        "verification_mode",
        "publication_authority",
        "whole_blueprint_verdict_authority",
    }
    raw = _exact_object(ticket, keys, label="targeted verification ticket")
    if raw["schema_version"] != TARGETED_CLAIM_TICKET_SCHEMA:
        raise ReviewContractError("targeted verification ticket schema is invalid")
    _safe_id(raw["ticket_id"], label="ticket_id", pattern=TICKET_ID_RE)
    _safe_id(raw["review_id"], label="review_id", pattern=REVIEW_ID_RE)
    _sha256(raw["snapshot_sha256"], label="snapshot_sha256")
    _safe_id(raw["route_id"], label="route_id")
    _sha256(raw["blueprint_sha256"], label="blueprint_sha256")
    _safe_id(
        raw["blueprint_item_id"],
        label="blueprint_item_id",
        pattern=re.compile(r"^pi_[0-9a-f]{24}$"),
    )
    claim = _exact_object(
        raw["claim"],
        {"blueprint_item_label", "claim_sha256", "reason"},
        label="targeted verification claim",
    )
    _safe_id(claim["blueprint_item_label"], label="claim.blueprint_item_label")
    _sha256(claim["claim_sha256"], label="claim.claim_sha256")
    _bounded_text(claim["reason"], label="claim.reason")
    if (
        raw["verification_mode"] != "targeted_nonpublishing"
        or raw["publication_authority"] is not False
        or raw["whole_blueprint_verdict_authority"] is not False
    ):
        raise ReviewContractError(
            "targeted verification ticket may not verify or publish the whole proof"
        )
    seed = {
        "review_id": raw["review_id"],
        "snapshot_sha256": raw["snapshot_sha256"],
        "route_id": raw["route_id"],
        "blueprint_sha256": raw["blueprint_sha256"],
        "blueprint_item_id": raw["blueprint_item_id"],
        "claim": raw["claim"],
    }
    expected_id = "claim_" + hashlib.sha256(canonical_json_bytes(seed)).hexdigest()[:32]
    if raw["ticket_id"] != expected_id:
        raise ReviewContractError("targeted verification ticket content address mismatch")
    return deepcopy(raw)


def _nullable_safe_id(value: Any, *, label: str) -> str | None:
    if value is None:
        return None
    return _safe_id(value, label=label)


def _validate_continuous_context_handoff(
    handoff: Mapping[str, Any],
) -> dict[str, Any]:
    raw = _exact_object(
        handoff, _CONTINUOUS_HANDOFF_KEYS, label="continuous context handoff"
    )
    if raw["schema_version"] != CONTINUOUS_CONTEXT_HANDOFF_SCHEMA:
        raise ReviewContractError("continuous context handoff schema is unsupported")
    purpose = raw["purpose"]
    if purpose not in {"context_guard", "owner_yield"}:
        raise ReviewContractError("continuous context handoff purpose is invalid")
    active_route = _exact_object(
        raw["active_route"],
        {"route_id", "core_bridge"},
        label="continuous handoff active_route",
    )
    pending = _exact_object(
        raw["pending"],
        {"verification_ticket_id", "advisor_checkpoint_id"},
        label="continuous handoff pending",
    )
    supervisor = _exact_object(
        raw["supervisor"],
        {
            "policy_id",
            "policy_sha256",
            "origin_wall_epoch",
            "next_review_ordinal",
            "next_review_wall_epoch",
            "next_renewal_ordinal",
            "next_renewal_wall_epoch",
            "state",
        },
        label="continuous handoff supervisor",
    )
    for name in (
        "origin_wall_epoch",
        "next_review_wall_epoch",
        "next_renewal_wall_epoch",
    ):
        value = supervisor[name]
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
        ):
            raise ReviewContractError(
                f"continuous handoff supervisor.{name} is invalid"
            )
    for name in ("next_review_ordinal", "next_renewal_ordinal"):
        value = supervisor[name]
        if type(value) is not int or value < 1:
            raise ReviewContractError(
                f"continuous handoff supervisor.{name} is invalid"
            )
    if supervisor["state"] not in {
        "active",
        "review_collecting",
        "review_running",
        "review_applying",
        "candidate_verification",
        "advisor_checkpoint_required",
        "paused_owner",
        "waiting_owner_advisor_decision",
        "operational_blocked",
        "completed",
    }:
        raise ReviewContractError("continuous handoff supervisor.state is invalid")
    normalized_supervisor = {
        **supervisor,
        "policy_id": _safe_id(
            supervisor["policy_id"], label="continuous handoff supervisor policy_id"
        ),
        "policy_sha256": _sha256(
            supervisor["policy_sha256"],
            label="continuous handoff supervisor policy_sha256",
        ),
    }

    latest = raw["latest_cohort_decision"]
    if latest is not None:
        latest = _exact_object(
            latest,
            {
                "schema_version",
                "review_id",
                "snapshot_sha256",
                "lane_actions",
                "global_action",
                "decision_sha256",
            },
            label="continuous handoff latest decision",
        )
        if latest["schema_version"] != "rethlas_cohort_review_decision_v1":
            raise ReviewContractError("continuous handoff decision schema is invalid")
        if latest["global_action"] not in {
            "continue_cohort",
            "verify_candidate",
            "seek_advisor",
            "operational_blocked",
        }:
            raise ReviewContractError("continuous handoff global action is invalid")
        actions = latest["lane_actions"]
        if not isinstance(actions, list) or len(actions) > 3:
            raise ReviewContractError("continuous handoff lane actions are invalid")
        normalized_actions = []
        lane_ids: list[str] = []
        thread_ids: list[str] = []
        for index, item in enumerate(actions):
            action = _exact_object(
                item,
                {
                    "lane_id",
                    "thread_id",
                    "route_id",
                    "verdict",
                    "next_action",
                    "redirect_to_lane_id",
                    "fatal_doubt",
                    "next_test",
                    "accepted_evidence_ids",
                },
                label=f"continuous handoff lane action[{index}]",
            )
            lane_id = _safe_id(
                action["lane_id"], label=f"continuous handoff lane action[{index}] id"
            )
            thread_id = _safe_id(
                action["thread_id"],
                label=f"continuous handoff lane action[{index}] thread",
            )
            if action["verdict"] not in {"green", "yellow", "red", "unclear"}:
                raise ReviewContractError("continuous handoff lane verdict is invalid")
            if action["next_action"] not in {
                "resume_same",
                "restart_same",
                "redirect",
                "retire",
                "verify",
            }:
                raise ReviewContractError("continuous handoff lane action is invalid")
            normalized_actions.append(
                {
                    **action,
                    "lane_id": lane_id,
                    "thread_id": thread_id,
                    "route_id": _safe_id(
                        action["route_id"],
                        label=f"continuous handoff lane action[{index}] route",
                    ),
                    "redirect_to_lane_id": _nullable_safe_id(
                        action["redirect_to_lane_id"],
                        label=f"continuous handoff lane action[{index}] redirect",
                    ),
                    "fatal_doubt": (
                        None
                        if action["fatal_doubt"] is None
                        else _bounded_text(
                            action["fatal_doubt"],
                            label=f"continuous handoff lane action[{index}] fatal doubt",
                            max_bytes=MAX_SHORT_TEXT_BYTES,
                        )
                    ),
                    "next_test": (
                        None
                        if action["next_test"] is None
                        else _bounded_text(
                            action["next_test"],
                            label=f"continuous handoff lane action[{index}] next test",
                            max_bytes=MAX_SHORT_TEXT_BYTES,
                        )
                    ),
                    "accepted_evidence_ids": _bounded_unique_ids(
                        action["accepted_evidence_ids"],
                        label=f"continuous handoff lane action[{index}] evidence",
                    ),
                }
            )
            lane_ids.append(lane_id)
            thread_ids.append(thread_id)
        if len(set(lane_ids)) != len(lane_ids) or len(set(thread_ids)) != len(
            thread_ids
        ):
            raise ReviewContractError("continuous handoff decision repeats a lane")
        latest = {
            **latest,
            "review_id": _safe_id(
                latest["review_id"], label="continuous handoff review id"
            ),
            "snapshot_sha256": _sha256(
                latest["snapshot_sha256"],
                label="continuous handoff snapshot digest",
            ),
            "decision_sha256": _sha256(
                latest["decision_sha256"],
                label="continuous handoff decision digest",
            ),
            "lane_actions": normalized_actions,
        }

    parked = raw["parked_lanes"]
    if not isinstance(parked, list) or len(parked) > 3:
        raise ReviewContractError("continuous handoff parked lanes are invalid")
    normalized_parked = []
    parked_ids: list[str] = []
    for index, item in enumerate(parked):
        lane = _exact_object(
            item,
            {
                "lane_id",
                "thread_id",
                "summary_sha256",
                "summary",
                "verdict",
                "next_action",
            },
            label=f"continuous handoff parked lane[{index}]",
        )
        lane_id = _safe_id(
            lane["lane_id"], label=f"continuous handoff parked lane[{index}] id"
        )
        summary = lane["summary"]
        if summary is not None:
            if not isinstance(summary, dict) or len(canonical_json_bytes(summary)) > 8192:
                raise ReviewContractError("continuous handoff parked summary is invalid")
            if {"transcript", "reasoning", "chain_of_thought", "messages"} & set(
                summary
            ):
                raise ReviewContractError(
                    "continuous handoff parked summary carries forbidden content"
                )
            summary = deepcopy(summary)
        summary_sha = _sha256(
            lane["summary_sha256"],
            label=f"continuous handoff parked lane[{index}] summary digest",
            nullable=True,
        )
        if (summary is None) != (summary_sha is None):
            raise ReviewContractError("continuous handoff parked summary is incomplete")
        if lane["verdict"] not in {None, "green", "yellow", "red", "unclear"}:
            raise ReviewContractError("continuous handoff parked verdict is invalid")
        if lane["next_action"] not in {
            None,
            "resume_same",
            "restart_same",
            "redirect",
            "retire",
            "verify",
        }:
            raise ReviewContractError("continuous handoff parked action is invalid")
        normalized_parked.append(
            {
                **lane,
                "lane_id": lane_id,
                "thread_id": _safe_id(
                    lane["thread_id"],
                    label=f"continuous handoff parked lane[{index}] thread",
                ),
                "summary_sha256": summary_sha,
                "summary": summary,
            }
        )
        parked_ids.append(lane_id)
    if len(set(parked_ids)) != len(parked_ids):
        raise ReviewContractError("continuous handoff repeats a parked lane")

    normalized = {
        "schema_version": CONTINUOUS_CONTEXT_HANDOFF_SCHEMA,
        "purpose": purpose,
        "run_id": _safe_id(raw["run_id"], label="continuous handoff run_id"),
        "problem_id": _safe_id(
            raw["problem_id"],
            label="continuous handoff problem_id",
            pattern=PROBLEM_ID_RE,
        ),
        "from_thread_epoch": _safe_id(
            raw["from_thread_epoch"], label="continuous handoff from_thread_epoch"
        ),
        "statement_sha256": _sha256(
            raw["statement_sha256"], label="continuous handoff statement digest"
        ),
        "blueprint_sha256": _sha256(
            raw["blueprint_sha256"],
            label="continuous handoff blueprint digest",
            nullable=True,
        ),
        "supervisor": normalized_supervisor,
        "latest_cohort_decision": latest,
        "parked_lanes": normalized_parked,
        "active_route": {
            "route_id": _safe_id(
                active_route["route_id"], label="continuous handoff route_id"
            ),
            "core_bridge": _bounded_text(
                active_route["core_bridge"], label="continuous handoff core bridge"
            ),
        },
        "new_record_ids": _bounded_unique_ids(
            raw["new_record_ids"],
            label="continuous handoff new record ids",
            maximum=MAX_HANDOFF_RECORD_IDS,
        ),
        "obligations": _bounded_obligations(
            raw["obligations"], label="continuous handoff obligations"
        ),
        "next_action": {
            key: _bounded_text(
                _exact_object(
                    raw["next_action"],
                    {"description", "test"},
                    label="continuous handoff next_action",
                )[key],
                label=f"continuous handoff next_action.{key}",
            )
            for key in ("description", "test")
        },
        "pending": {
            "verification_ticket_id": _nullable_safe_id(
                pending["verification_ticket_id"],
                label="continuous pending verification ticket",
            ),
            "advisor_checkpoint_id": _nullable_safe_id(
                pending["advisor_checkpoint_id"],
                label="continuous pending advisor checkpoint",
            ),
        },
    }
    if len(canonical_json_bytes(normalized)) > MAX_CONTEXT_HANDOFF_BYTES:
        raise ReviewContractError("continuous context handoff exceeds 32 KiB")
    return normalized


def validate_context_handoff(handoff: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a compact state transfer; transcripts/reasoning are forbidden."""

    if (
        isinstance(handoff, dict)
        and handoff.get("schema_version") == CONTINUOUS_CONTEXT_HANDOFF_SCHEMA
    ):
        return _validate_continuous_context_handoff(handoff)
    raw = _exact_object(handoff, _HANDOFF_KEYS, label="context handoff")
    if raw["schema_version"] != CONTEXT_HANDOFF_SCHEMA:
        raise ReviewContractError("context handoff schema_version is unsupported")
    normalized: dict[str, Any] = {
        "schema_version": CONTEXT_HANDOFF_SCHEMA,
        "purpose": raw["purpose"],
        "run_id": _safe_id(raw["run_id"], label="handoff run_id"),
        "problem_id": _safe_id(
            raw["problem_id"], label="handoff problem_id", pattern=PROBLEM_ID_RE
        ),
        "from_thread_epoch": _safe_id(
            raw["from_thread_epoch"], label="handoff from_thread_epoch"
        ),
        "statement_sha256": _sha256(
            raw["statement_sha256"], label="handoff statement_sha256"
        ),
        "blueprint_sha256": _sha256(
            raw["blueprint_sha256"], label="handoff blueprint_sha256", nullable=True
        ),
    }
    if not isinstance(normalized["purpose"], str) or normalized["purpose"] not in {
        "context_guard",
        "owner_yield",
        "cycle_close",
    }:
        raise ReviewContractError("context handoff purpose is invalid")
    cadence = _exact_object(
        raw["cadence"],
        {
            "phase",
            "cycle_started_at_utc",
            "minute60_at_utc",
            "minute120_at_utc",
            "close_at_utc",
            "hard_stop_at_utc",
        },
        label="handoff cadence",
    )
    phase = cadence["phase"]
    if phase not in {
        "work_0_60",
        "review_60",
        "work_60_120",
        "review_120",
        "work_120_150",
        "hard_stop",
    }:
        raise ReviewContractError("handoff cadence.phase is invalid")
    normalized_cadence = {
        "phase": phase,
        **{
            key: _bounded_text(cadence[key], label=f"handoff cadence.{key}", max_bytes=64)
            for key in (
                "cycle_started_at_utc",
                "minute60_at_utc",
                "minute120_at_utc",
                "close_at_utc",
                "hard_stop_at_utc",
            )
        },
    }
    parsed_deadlines: list[datetime] = []
    for key in (
        "cycle_started_at_utc",
        "minute60_at_utc",
        "minute120_at_utc",
        "close_at_utc",
        "hard_stop_at_utc",
    ):
        try:
            parsed = datetime.fromisoformat(normalized_cadence[key])
        except ValueError as exc:
            raise ReviewContractError(f"handoff cadence.{key} is not ISO-8601") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ReviewContractError(f"handoff cadence.{key} must be timezone-aware")
        parsed_deadlines.append(parsed)
    started, minute60, minute120, close_at, hard_stop = parsed_deadlines
    if (
        (minute60 - started).total_seconds() != 3600
        or (minute120 - started).total_seconds() != 7200
        or (close_at - started).total_seconds() != 8820
        or (hard_stop - started).total_seconds() != 9000
    ):
        raise ReviewContractError("handoff cadence deadlines do not encode 60/120/150")
    normalized["cadence"] = normalized_cadence
    active_route = _exact_object(
        raw["active_route"], {"route_id", "core_bridge"}, label="handoff active_route"
    )
    normalized["active_route"] = {
        "route_id": _safe_id(active_route["route_id"], label="handoff route_id"),
        "core_bridge": _bounded_text(
            active_route["core_bridge"], label="handoff core_bridge"
        ),
    }
    last_review_raw = raw["last_review"]
    if last_review_raw is None:
        last_review = None
    else:
        last = _exact_object(
            last_review_raw,
            {
                "review_id",
                "snapshot_sha256",
                "route_id",
                "verdict",
                "effective_verdict",
                "allowed_action",
                "next_route_id",
                "fallback_evidence_record_ids",
            },
            label="handoff last_review",
        )
        if last["verdict"] not in REVIEW_VERDICTS or last["effective_verdict"] not in REVIEW_VERDICTS:
            raise ReviewContractError("handoff last_review verdict is invalid")
        if (
            last["verdict"] == "red" and last["effective_verdict"] != "red"
        ) or (
            last["verdict"] == "green"
            and last["effective_verdict"] not in {"green", "red"}
        ) or (
            last["verdict"] == "yellow"
            and last["effective_verdict"] not in {"yellow", "red"}
        ):
            raise ReviewContractError("handoff last_review weakens its raw verdict")
        if last["allowed_action"] != EFFECTIVE_ALLOWED_ACTION[last["effective_verdict"]]:
            raise ReviewContractError("handoff allowed_action conflicts with effective verdict")
        reviewed_route_id = _safe_id(
            last["route_id"], label="handoff reviewed route_id"
        )
        next_route_id = _nullable_safe_id(
            last["next_route_id"], label="handoff next route_id"
        )
        fallback_ids = _bounded_unique_ids(
            last["fallback_evidence_record_ids"],
            label="handoff fallback evidence ids",
        )
        if last["effective_verdict"] != "red":
            if next_route_id is not None or fallback_ids:
                raise ReviewContractError("non-red handoff cannot switch routes")
        elif next_route_id is None:
            if fallback_ids:
                raise ReviewContractError("red handoff without fallback route has evidence")
        elif next_route_id == reviewed_route_id or not fallback_ids:
            raise ReviewContractError("red handoff route switch lacks distinct evidence")
        last_review = {
            "review_id": _safe_id(last["review_id"], label="handoff review_id", pattern=REVIEW_ID_RE),
            "snapshot_sha256": _sha256(last["snapshot_sha256"], label="handoff snapshot_sha256"),
            "route_id": reviewed_route_id,
            "verdict": last["verdict"],
            "effective_verdict": last["effective_verdict"],
            "allowed_action": last["allowed_action"],
            "next_route_id": next_route_id,
            "fallback_evidence_record_ids": fallback_ids,
        }
    normalized["last_review"] = last_review
    normalized["new_record_ids"] = _bounded_unique_ids(
        raw["new_record_ids"], label="handoff new_record_ids", maximum=MAX_HANDOFF_RECORD_IDS
    )
    yellow_streak = raw["yellow_streak"]
    if type(yellow_streak) is not int or yellow_streak < 0 or yellow_streak > 2:
        raise ReviewContractError("handoff yellow_streak must be 0, 1, or 2")
    route_frozen = raw["route_frozen"]
    if type(route_frozen) is not bool:
        raise ReviewContractError("handoff route_frozen must be boolean")
    if last_review is not None:
        switched_after_red = (
            last_review["effective_verdict"] == "red"
            and last_review["next_route_id"] is not None
        )
        expected_frozen = (
            last_review["effective_verdict"] == "red" and not switched_after_red
        )
        if route_frozen != expected_frozen:
            raise ReviewContractError("handoff route_frozen conflicts with last review")
        expected_active_route = (
            last_review["next_route_id"]
            if switched_after_red
            else last_review["route_id"]
        )
        if normalized["active_route"]["route_id"] != expected_active_route:
            raise ReviewContractError("handoff active route conflicts with review transition")
        expected_streak = {
            ("green", "green"): 0,
            ("yellow", "yellow"): 1,
            ("red", "red"): 0,
            ("yellow", "red"): 2,
            ("green", "red"): 0,
        }[(last_review["verdict"], last_review["effective_verdict"])]
        if switched_after_red:
            expected_streak = 0
        if yellow_streak != expected_streak:
            raise ReviewContractError("handoff yellow streak conflicts with last review")
    elif yellow_streak != 0 or route_frozen:
        raise ReviewContractError("handoff without a review cannot carry review state")
    normalized["yellow_streak"] = yellow_streak
    normalized["route_frozen"] = route_frozen
    if route_frozen and last_review is None:
        raise ReviewContractError("a frozen handoff route requires its last red review")
    pending = _exact_object(
        raw["pending"],
        {"verification_ticket_id", "advisor_checkpoint_id"},
        label="handoff pending",
    )
    normalized["pending"] = {
        "verification_ticket_id": _nullable_safe_id(
            pending["verification_ticket_id"], label="pending verification_ticket_id"
        ),
        "advisor_checkpoint_id": _nullable_safe_id(
            pending["advisor_checkpoint_id"], label="pending advisor_checkpoint_id"
        ),
    }
    obligations_raw = raw["obligations"]
    if not isinstance(obligations_raw, list) or len(obligations_raw) > MAX_HANDOFF_OBLIGATIONS:
        raise ReviewContractError("handoff obligations must be a bounded array")
    obligations = [
        _bounded_text(item, label=f"handoff obligations[{index}]", max_bytes=MAX_SHORT_TEXT_BYTES)
        for index, item in enumerate(obligations_raw)
    ]
    if len(set(obligations)) != len(obligations):
        raise ReviewContractError("handoff obligations must be unique")
    normalized["obligations"] = obligations
    next_action = _exact_object(
        raw["next_action"], {"description", "test"}, label="handoff next_action"
    )
    normalized["next_action"] = {
        "description": _bounded_text(
            next_action["description"], label="handoff next_action.description"
        ),
        "test": _bounded_text(next_action["test"], label="handoff next_action.test"),
    }
    forbidden_terms = {"transcript", "reasoning", "chain_of_thought", "messages"}
    if forbidden_terms & set(raw):  # Defensive if schema is relaxed in the future.
        raise ReviewContractError("context handoff may not carry transcript/reasoning")
    if len(canonical_json_bytes(normalized)) > MAX_CONTEXT_HANDOFF_BYTES:
        raise ReviewContractError("context handoff exceeds 32 KiB")
    return normalized


def handoff_sha256(handoff: Mapping[str, Any]) -> str:
    normalized = validate_context_handoff(handoff)
    return hashlib.sha256(canonical_json_bytes(normalized)).hexdigest()


def handoff_id(handoff: Mapping[str, Any]) -> str:
    return f"handoff_{handoff_sha256(handoff)}"
