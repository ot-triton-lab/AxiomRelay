from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import requests

try:
    from jsonschema import Draft202012Validator
except ImportError:  # pragma: no cover - dependency managed by requirements
    Draft202012Validator = None  # type: ignore[assignment]

try:  # MCP SDK 1.x
    from mcp.server.fastmcp import FastMCP
except ImportError:  # MCP SDK 2.x
    try:
        from mcp.server.mcpserver import MCPServer as FastMCP
    except ImportError:  # pragma: no cover - dependency managed by requirements
        FastMCP = None  # type: ignore[assignment]


REPO_ROOT = Path(__file__).resolve().parents[1]
MEMORY_ROOT = REPO_ROOT / "memory"
RESULTS_ROOT = REPO_ROOT / "results"
SCHEMA_PATH = REPO_ROOT / "schemas" / "verification_output.schema.json"

MATLAS_SEARCH_URL = "https://matlas.ai/api/search"
LEGACY_ARXIV_THEOREM_URL = "https://leansearch.net/thm/search"
THEOREM_SEARCH_TASK = (
    "Given a math statement, retrieve useful references, such as theorems, "
    "lemmas, and definitions, that are useful for solving the given problem."
)
MAX_EXTERNAL_QUERY_UTF8_BYTES = 8_192
MAX_EXTERNAL_SUCCESS_UTF8_BYTES = 32_768
MAX_EXTERNAL_RAW_RESPONSE_BYTES = 262_144


class _ExternalResponseTooLarge(ValueError):
    pass


class _ExternalResponseCloseError(requests.RequestException):
    pass


def _normalize_external_query(query: Any) -> str:
    if not isinstance(query, str):
        raise ValueError("query must be a string")
    normalized = query.strip()
    if not normalized:
        raise ValueError("query must be non-empty")
    try:
        encoded = normalized.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ValueError("query must be valid UTF-8") from error
    if len(encoded) > MAX_EXTERNAL_QUERY_UTF8_BYTES:
        raise ValueError("query exceeds external retrieval byte limit")
    return normalized


def _validate_external_result_count(num_results: Any) -> int:
    if (
        isinstance(num_results, bool)
        or not isinstance(num_results, int)
        or not 1 <= num_results <= 200
    ):
        raise ValueError("num_results must be an integer between 1 and 200")
    return num_results


def _external_fields_are_utf8(item: Dict[str, str], fields: Iterable[str]) -> bool:
    try:
        for field in fields:
            item[field].encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _read_bounded_external_json(response: requests.Response) -> Any:
    headers = getattr(response, "headers", {})
    content_length = headers.get("Content-Length") if hasattr(headers, "get") else None
    if isinstance(content_length, str) and content_length.isdigit():
        if int(content_length) > MAX_EXTERNAL_RAW_RESPONSE_BYTES:
            raise _ExternalResponseTooLarge

    iter_content = getattr(response, "iter_content", None)
    if not callable(iter_content):
        return response.json()

    chunks: List[bytes] = []
    total = 0
    for chunk in iter_content(chunk_size=16_384):
        if not chunk:
            continue
        if not isinstance(chunk, bytes):
            raise ValueError("external response chunk must be bytes")
        total += len(chunk)
        if total > MAX_EXTERNAL_RAW_RESPONSE_BYTES:
            raise _ExternalResponseTooLarge
        chunks.append(chunk)
    return json.loads(b"".join(chunks))


CHANNEL_FILES: Dict[str, str] = {
    "statement_checks": "statement_checks.jsonl",
    "reference_checks": "reference_checks.jsonl",
    "verification_reports": "verification_reports.jsonl",
    "failed_checks": "failed_checks.jsonl",
    "events": "events.jsonl",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sanitize_run_id(raw: str) -> str:
    cleaned = re.sub(r"\s+", "_", str(raw).strip())
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", cleaned)
    cleaned = re.sub(r"_+", "_", cleaned).strip("._")
    return cleaned or "run"


def _run_dir(run_id: str) -> Path:
    return MEMORY_ROOT / sanitize_run_id(run_id)


def _channel_path(run_id: str, channel: str) -> Path:
    if channel not in CHANNEL_FILES:
        allowed = ", ".join(sorted(CHANNEL_FILES))
        raise ValueError(f"Unknown channel '{channel}'. Allowed channels: {allowed}")
    return _run_dir(run_id) / CHANNEL_FILES[channel]


def _append_jsonl(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _record_id(value: Dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()[:16]


def _iter_jsonl(path: Path) -> Iterable[Dict[str, Any]]:
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                yield payload


def search_matlas_theorems(
    query: str,
    num_results: int = 10,
    endpoint: str = MATLAS_SEARCH_URL,
    timeout_seconds: int = 30,
) -> Dict[str, Any]:
    normalized_query = _normalize_external_query(query)
    num_results = _validate_external_result_count(num_results)

    # Matlas 0.1 accepts between 10 and 200 results. Preserve a smaller MCP
    # request by querying for ten and truncating only the returned list.
    upstream_count = max(10, num_results)
    payload = {
        "query": normalized_query,
        "num_results": upstream_count,
    }
    envelope: Dict[str, Any] = {
        "schema_version": "rethlas_external_retrieval_v1",
        "provider": "matlas_official_v0_1",
        "provider_protocol": "matlas_openapi_0_1_0",
        "endpoint": endpoint,
        "query": normalized_query,
        "requested_count": num_results,
        "count": 0,
        "results": [],
        "scope": "published_mathematical_statements",
        "mathematical_evidence_authority": False,
        "fallback_used": False,
    }
    try:
        response = requests.post(
            endpoint,
            json=payload,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "rethlas/1.0",
            },
            timeout=timeout_seconds,
            stream=True,
        )
        primary_error_pending = True
        try:
            response.raise_for_status()
            data = _read_bounded_external_json(response)
            primary_error_pending = False
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                try:
                    close()
                except Exception as error:
                    if not primary_error_pending:
                        raise _ExternalResponseCloseError from error
    except _ExternalResponseTooLarge:
        return {**envelope, "retrieval_status": "error", "error": "response_too_large"}
    except _ExternalResponseCloseError:
        return {
            **envelope,
            "retrieval_status": "error",
            "error": "network response_close_failed",
        }
    except requests.HTTPError as error:
        status = error.response.status_code if error.response is not None else "unknown"
        return {
            **envelope,
            "retrieval_status": "error",
            "error": f"http {status}",
        }
    except ValueError:
        return {
            **envelope,
            "retrieval_status": "error",
            "error": "invalid_json",
        }
    except requests.RequestException as error:
        return {
            **envelope,
            "retrieval_status": "error",
            "error": f"network {type(error).__name__}",
        }
    if not isinstance(data, list):
        return {
            **envelope,
            "retrieval_status": "error",
            "error": f"invalid_response_type:{type(data).__name__}",
        }

    required_string_fields = (
        "entity_name",
        "doi",
        "title",
        "authors",
        "journal",
        "year",
        "statement",
        "candidate_id",
    )
    normalized: List[Dict[str, str]] = []
    for index, item in enumerate(data):
        if not isinstance(item, dict):
            return {
                **envelope,
                "retrieval_status": "error",
                "error": f"invalid_result:{index}:not_object",
            }
        source_type = item.get("type")
        if not isinstance(source_type, str) or source_type not in {"book", "paper"}:
            return {
                **envelope,
                "retrieval_status": "error",
                "error": f"invalid_result:{index}:type",
            }
        if any(
            not isinstance(item.get(field), str) for field in required_string_fields
        ):
            return {
                **envelope,
                "retrieval_status": "error",
                "error": f"invalid_result:{index}:fields",
            }
        if not _external_fields_are_utf8(item, required_string_fields):
            return {
                **envelope,
                "retrieval_status": "error",
                "error": f"invalid_result:{index}:encoding",
            }
        normalized.append(
            {
                "type": source_type,
                **{field: item[field] for field in required_string_fields},
            }
        )

    normalized = normalized[:num_results]
    result = {
        **envelope,
        "count": len(normalized),
        "results": normalized,
        "retrieval_status": "ok",
    }
    if len(_canonical_json(result).encode("utf-8")) > MAX_EXTERNAL_SUCCESS_UTF8_BYTES:
        return {**envelope, "retrieval_status": "error", "error": "response_too_large"}
    return result


def search_arxiv_theorems(
    query: str,
    num_results: int = 10,
    endpoint: str = LEGACY_ARXIV_THEOREM_URL,
    timeout_seconds: int = 30,
) -> Dict[str, Any]:
    """Query the historical arXiv theorem service without implicit fallback."""

    normalized_query = _normalize_external_query(query)
    num_results = _validate_external_result_count(num_results)

    envelope: Dict[str, Any] = {
        "schema_version": "rethlas_external_retrieval_v1",
        "provider": "danus_legacy_arxiv_theorem_v1",
        "provider_protocol": "danus_legacy_arxiv_theorem_search_v1",
        "endpoint": endpoint,
        "query": normalized_query,
        "requested_count": num_results,
        "count": 0,
        "results": [],
        "scope": "arxiv_theorem_snippets",
        "mathematical_evidence_authority": False,
        "fallback_used": False,
    }
    try:
        response = requests.post(
            endpoint,
            json={
                "query": normalized_query,
                "task": THEOREM_SEARCH_TASK,
                "num_results": num_results,
            },
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "rethlas/1.0",
            },
            timeout=timeout_seconds,
            stream=True,
        )
        primary_error_pending = True
        try:
            response.raise_for_status()
            data = _read_bounded_external_json(response)
            primary_error_pending = False
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                try:
                    close()
                except Exception as error:
                    if not primary_error_pending:
                        raise _ExternalResponseCloseError from error
    except _ExternalResponseTooLarge:
        return {**envelope, "retrieval_status": "error", "error": "response_too_large"}
    except _ExternalResponseCloseError:
        return {
            **envelope,
            "retrieval_status": "error",
            "error": "network response_close_failed",
        }
    except requests.HTTPError as error:
        status = error.response.status_code if error.response is not None else "unknown"
        return {**envelope, "retrieval_status": "error", "error": f"http {status}"}
    except ValueError:
        return {**envelope, "retrieval_status": "error", "error": "invalid_json"}
    except requests.RequestException as error:
        return {
            **envelope,
            "retrieval_status": "error",
            "error": f"network {type(error).__name__}",
        }
    if not isinstance(data, list):
        return {
            **envelope,
            "retrieval_status": "error",
            "error": f"invalid_response_type:{type(data).__name__}",
        }

    required_fields = ("title", "theorem", "arxiv_id", "theorem_id")
    normalized: List[Dict[str, str]] = []
    for index, item in enumerate(data):
        if not isinstance(item, dict) or any(
            not isinstance(item.get(field), str) for field in required_fields
        ):
            return {
                **envelope,
                "retrieval_status": "error",
                "error": f"invalid_result:{index}",
            }
        if not _external_fields_are_utf8(item, required_fields):
            return {
                **envelope,
                "retrieval_status": "error",
                "error": f"invalid_result:{index}:encoding",
            }
        normalized.append({field: item[field] for field in required_fields})

    normalized = normalized[:num_results]
    result = {
        **envelope,
        "count": len(normalized),
        "results": normalized,
        "retrieval_status": "ok",
    }
    if len(_canonical_json(result).encode("utf-8")) > MAX_EXTERNAL_SUCCESS_UTF8_BYTES:
        return {**envelope, "retrieval_status": "error", "error": "response_too_large"}
    return result


def memory_init(run_id: str, meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    sanitized_run_id = sanitize_run_id(run_id)
    run_dir = _run_dir(sanitized_run_id)
    run_dir.mkdir(parents=True, exist_ok=True)

    created_files: Dict[str, str] = {}
    for channel, filename in CHANNEL_FILES.items():
        channel_path = run_dir / filename
        channel_path.touch(exist_ok=True)
        created_files[channel] = str(channel_path)

    meta_path = run_dir / "meta.json"
    existing_meta: Dict[str, Any] = {}
    if meta_path.exists() and meta_path.stat().st_size > 0:
        with meta_path.open("r", encoding="utf-8") as handle:
            loaded = json.load(handle)
            if isinstance(loaded, dict):
                existing_meta = loaded

    merged_meta: Dict[str, Any] = {
        "run_id": sanitized_run_id,
        "created_at_utc": existing_meta.get("created_at_utc", _utc_now()),
        "updated_at_utc": _utc_now(),
    }
    merged_meta.update(existing_meta)
    if meta:
        merged_meta.update(meta)

    with meta_path.open("w", encoding="utf-8") as handle:
        json.dump(merged_meta, handle, indent=2, ensure_ascii=False)

    return {
        "run_id": sanitized_run_id,
        "memory_dir": str(run_dir),
        "meta_path": str(meta_path),
        "channels": created_files,
    }


def memory_append(run_id: str, channel: str, record: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(record, dict):
        raise ValueError("record must be a JSON object")

    resolved_run_id = sanitize_run_id(run_id)
    memory_init(resolved_run_id)

    entry = {
        "timestamp_utc": _utc_now(),
        "channel": channel,
        "record": record,
    }
    entry["record_id"] = _record_id(entry)
    target = _channel_path(resolved_run_id, channel)
    _append_jsonl(target, entry)

    if channel != "events":
        _append_jsonl(
            _channel_path(resolved_run_id, "events"),
            {
                "timestamp_utc": _utc_now(),
                "event_type": "memory_append",
                "channel": channel,
            },
        )

    return {
        "status": "ok",
        "run_id": resolved_run_id,
        "channel": channel,
        "path": str(target),
        "record_id": entry["record_id"],
        "timestamp_utc": entry["timestamp_utc"],
    }


def memory_query(
    run_id: str,
    channel: str,
    filters: Optional[Dict[str, Any]] = None,
    contains: Optional[str] = None,
    limit: int = 100,
    reverse: bool = True,
    max_chars: int = 50_000,
) -> Dict[str, Any]:
    if limit <= 0:
        raise ValueError("limit must be > 0")
    if max_chars <= 0:
        raise ValueError("max_chars must be > 0")

    resolved_run_id = sanitize_run_id(run_id)
    path = _channel_path(resolved_run_id, channel)
    items = list(_iter_jsonl(path))

    if filters:
        filtered: List[Dict[str, Any]] = []
        for item in items:
            if all(item.get(key) == value for key, value in filters.items()):
                filtered.append(item)
        items = filtered

    if contains:
        needle = contains.lower()
        items = [
            item
            for item in items
            if needle in json.dumps(item, ensure_ascii=False).lower()
        ]

    if reverse:
        items = list(reversed(items))

    corpus_count = len(items)
    candidates = items[:limit]
    selected: List[Dict[str, Any]] = []
    omitted_ids: List[str] = []
    returned_chars = 0
    for item in candidates:
        item_chars = len(_canonical_json(item))
        if returned_chars + item_chars > max_chars:
            omitted_ids.append(str(item.get("record_id") or _record_id(item)))
            continue
        selected.append(item)
        returned_chars += item_chars

    for item in items[limit:]:
        omitted_ids.append(str(item.get("record_id") or _record_id(item)))

    truncated = bool(omitted_ids)
    return {
        "run_id": resolved_run_id,
        "channel": channel,
        "count": len(selected),
        "corpus_count": corpus_count,
        "items": selected,
        "complete": not truncated,
        "truncated": truncated,
        "omitted_count": len(omitted_ids),
        "omitted_ids": omitted_ids,
        "max_chars": max_chars,
        "returned_chars": returned_chars,
    }


def _load_schema() -> Dict[str, Any]:
    if not SCHEMA_PATH.exists():
        raise FileNotFoundError(f"Schema not found: {SCHEMA_PATH}")
    with SCHEMA_PATH.open("r", encoding="utf-8") as handle:
        schema = json.load(handle)
    if not isinstance(schema, dict):
        raise ValueError("schema must be a JSON object")
    return schema


def validate_verification_output(payload: Dict[str, Any]) -> Dict[str, Any]:
    errors: List[str] = []

    try:
        schema = _load_schema()
    except Exception as exc:
        return {"valid": False, "errors": [f"schema load failed: {exc}"]}

    if Draft202012Validator is None:
        errors.append("jsonschema dependency is missing; cannot validate schema")
    else:
        validator = Draft202012Validator(schema)
        for error in validator.iter_errors(payload):
            path = ".".join(str(part) for part in error.path)
            if path:
                errors.append(f"schema error at '{path}': {error.message}")
            else:
                errors.append(f"schema error: {error.message}")

    report = payload.get("verification_report")
    verdict = payload.get("verdict")
    repair_hints = payload.get("repair_hints")

    critical_errors = []
    gaps = []
    if isinstance(report, dict):
        if isinstance(report.get("critical_errors"), list):
            critical_errors = report["critical_errors"]
        if isinstance(report.get("gaps"), list):
            gaps = report["gaps"]

    has_any_finding = len(critical_errors) + len(gaps) > 0

    if verdict == "correct":
        if has_any_finding:
            errors.append(
                "verdict='correct' is invalid when critical_errors or gaps are non-empty"
            )
        if repair_hints != "":
            errors.append("repair_hints must be empty when verdict='correct'")
    elif verdict == "wrong":
        if not has_any_finding:
            errors.append("verdict='wrong' requires at least one critical error or gap")
        if not isinstance(repair_hints, str) or not repair_hints.strip():
            errors.append("repair_hints must be non-empty when verdict='wrong'")
    else:
        errors.append("verdict must be 'correct' or 'wrong'")

    return {"valid": len(errors) == 0, "errors": errors}


def write_verification_output(run_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    validation = validate_verification_output(payload)
    if not validation["valid"]:
        raise ValueError(
            "verification output validation failed: " + "; ".join(validation["errors"])
        )

    resolved_run_id = sanitize_run_id(run_id)
    output_dir = RESULTS_ROOT / resolved_run_id
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / "verification.json"

    with output_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)

    memory_init(resolved_run_id)
    memory_append(
        resolved_run_id,
        "verification_reports",
        {
            "event_type": "final_report_written",
            "output_path": str(output_path),
            "verdict": payload.get("verdict"),
        },
    )

    return {
        "status": "ok",
        "run_id": resolved_run_id,
        "output_path": str(output_path),
    }


def build_mcp_app() -> Optional[Any]:
    if FastMCP is None:
        return None

    app = FastMCP("verification_agent")

    @app.tool(name="search_matlas_theorems")
    def _tool_search_matlas_theorems(
        query: str, num_results: int = 10
    ) -> Dict[str, Any]:
        """Search official Matlas for published mathematical statements."""
        return search_matlas_theorems(query=query, num_results=num_results)

    @app.tool(name="search_arxiv_theorems")
    def _tool_search_arxiv_theorems(
        query: str, num_results: int = 10
    ) -> Dict[str, Any]:
        """Search the separate legacy arXiv theorem service."""
        return search_arxiv_theorems(query=query, num_results=num_results)

    @app.tool(name="memory_init")
    def _tool_memory_init(
        run_id: str, meta: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        return memory_init(run_id=run_id, meta=meta)

    @app.tool(name="memory_append")
    def _tool_memory_append(
        run_id: str, channel: str, record: Dict[str, Any]
    ) -> Dict[str, Any]:
        return memory_append(run_id=run_id, channel=channel, record=record)

    @app.tool(name="memory_query")
    def _tool_memory_query(
        run_id: str,
        channel: str,
        filters: Optional[Dict[str, Any]] = None,
        contains: Optional[str] = None,
        limit: int = 100,
        reverse: bool = True,
        max_chars: int = 50_000,
    ) -> Dict[str, Any]:
        return memory_query(
            run_id=run_id,
            channel=channel,
            filters=filters,
            contains=contains,
            limit=limit,
            reverse=reverse,
            max_chars=max_chars,
        )

    return app


APP = build_mcp_app()


def main() -> None:
    if APP is None:
        raise SystemExit(
            "the official MCP SDK is missing or incompatible. Install "
            "dependencies from mcp/requirements.txt first."
        )
    APP.run()


if __name__ == "__main__":
    main()
