from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest
import requests


MODULE_PATH = Path(__file__).resolve().parents[1] / "mcp" / "server.py"
SPEC = importlib.util.spec_from_file_location(
    "rethlas_verification_mcp_search", MODULE_PATH
)
assert SPEC is not None and SPEC.loader is not None
mcp_server = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mcp_server)


MATLAS_RESULT = {
    "type": "paper",
    "entity_name": "Theorem 1.2",
    "doi": "10.1000/example",
    "title": "An Example Paper",
    "authors": "Ada Author; Emmy Example",
    "journal": "Journal of Examples",
    "year": "2026",
    "statement": "Every example has the required property.",
    "candidate_id": "candidate-1",
}


class _Response:
    def __init__(self, payload: Any) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> Any:
        return self._payload


class _StreamingResponse:
    def __init__(
        self, chunks: list[bytes], *, content_length: int | None = None
    ) -> None:
        self._chunks = chunks
        self.headers = (
            {"Content-Length": str(content_length)}
            if content_length is not None
            else {}
        )
        self.closed = False

    def raise_for_status(self) -> None:
        return None

    def iter_content(self, *, chunk_size: int):
        assert chunk_size == 16_384
        yield from self._chunks

    def close(self) -> None:
        self.closed = True


class _CloseFailingResponse(_Response):
    def __init__(self, payload: Any, *, status_code: int | None = None) -> None:
        super().__init__(payload)
        self._status_code = status_code

    def raise_for_status(self) -> None:
        if self._status_code is None:
            return
        response = requests.Response()
        response.status_code = self._status_code
        raise requests.HTTPError(response=response)

    def close(self) -> None:
        raise RuntimeError("close failed")


class _IterFailingCloseResponse:
    headers: dict[str, str] = {}

    def raise_for_status(self) -> None:
        return None

    def iter_content(self, *, chunk_size: int):
        assert chunk_size == 16_384
        raise requests.exceptions.ChunkedEncodingError("truncated chunk")
        yield b""  # pragma: no cover - keeps this a generator

    def close(self) -> None:
        raise RuntimeError("close failed")


def test_official_matlas_uses_v0_1_contract_and_truncates_small_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, Any], dict[str, str], int, bool]] = []

    def post(
        url: str,
        *,
        json: dict[str, Any],
        headers: dict[str, str],
        timeout: int,
        stream: bool,
    ) -> _Response:
        calls.append((url, json, headers, timeout, stream))
        second = {**MATLAS_RESULT, "candidate_id": "candidate-2"}
        return _Response([{**MATLAS_RESULT, "ignored_upstream_field": "drop"}, second])

    monkeypatch.setattr(mcp_server.requests, "post", post)
    result = mcp_server.search_matlas_theorems(
        "  a bounded statement  ",
        num_results=1,
        timeout_seconds=7,
    )

    assert calls == [
        (
            "https://matlas.ai/api/search",
            {"query": "a bounded statement", "num_results": 10},
            {
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "rethlas/1.0",
            },
            7,
            True,
        )
    ]
    assert result == {
        "schema_version": "rethlas_external_retrieval_v1",
        "provider": "matlas_official_v0_1",
        "provider_protocol": "matlas_openapi_0_1_0",
        "endpoint": "https://matlas.ai/api/search",
        "query": "a bounded statement",
        "requested_count": 1,
        "count": 1,
        "results": [MATLAS_RESULT],
        "scope": "published_mathematical_statements",
        "mathematical_evidence_authority": False,
        "fallback_used": False,
        "retrieval_status": "ok",
    }
    assert set(result["results"][0]) == {
        "type",
        "entity_name",
        "doi",
        "title",
        "authors",
        "journal",
        "year",
        "statement",
        "candidate_id",
    }


def test_official_matlas_forwards_maximum_upstream_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payloads: list[dict[str, Any]] = []

    def post(url: str, **kwargs: Any) -> _Response:
        assert url == mcp_server.MATLAS_SEARCH_URL
        payloads.append(kwargs["json"])
        return _Response([])

    monkeypatch.setattr(mcp_server.requests, "post", post)
    result = mcp_server.search_matlas_theorems("statement", num_results=200)

    assert payloads == [{"query": "statement", "num_results": 200}]
    assert result["retrieval_status"] == "ok"


@pytest.mark.parametrize("num_results", [0, 201])
def test_search_providers_reject_counts_outside_bounded_surface(
    monkeypatch: pytest.MonkeyPatch,
    num_results: int,
) -> None:
    def unexpected_post(*args: Any, **kwargs: Any) -> _Response:
        pytest.fail("invalid requests must not reach either provider")

    monkeypatch.setattr(mcp_server.requests, "post", unexpected_post)
    with pytest.raises(ValueError, match="between 1 and 200"):
        mcp_server.search_matlas_theorems("statement", num_results=num_results)
    with pytest.raises(ValueError, match="between 1 and 200"):
        mcp_server.search_arxiv_theorems("statement", num_results=num_results)


@pytest.mark.parametrize("num_results", [True, 1.5, "2", None])
def test_search_providers_reject_noninteger_counts_before_network(
    monkeypatch: pytest.MonkeyPatch,
    num_results: Any,
) -> None:
    monkeypatch.setattr(
        mcp_server.requests,
        "post",
        lambda *args, **kwargs: pytest.fail("invalid count reached provider"),
    )

    with pytest.raises(ValueError, match="integer between 1 and 200"):
        mcp_server.search_matlas_theorems("statement", num_results=num_results)
    with pytest.raises(ValueError, match="integer between 1 and 200"):
        mcp_server.search_arxiv_theorems("statement", num_results=num_results)


@pytest.mark.parametrize("query", [None, 123, b"statement", "\ud800"])
def test_search_providers_reject_non_utf8_string_queries_before_network(
    monkeypatch: pytest.MonkeyPatch,
    query: Any,
) -> None:
    monkeypatch.setattr(
        mcp_server.requests,
        "post",
        lambda *args, **kwargs: pytest.fail("invalid query reached provider"),
    )

    with pytest.raises(ValueError, match="query must"):
        mcp_server.search_matlas_theorems(query)
    with pytest.raises(ValueError, match="query must"):
        mcp_server.search_arxiv_theorems(query)


def test_search_providers_reject_oversized_query_before_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_post(*args: Any, **kwargs: Any) -> _Response:
        pytest.fail("oversized query reached a provider")

    monkeypatch.setattr(mcp_server.requests, "post", unexpected_post)
    query = "x" * (mcp_server.MAX_EXTERNAL_QUERY_UTF8_BYTES + 1)

    with pytest.raises(ValueError, match="byte limit"):
        mcp_server.search_matlas_theorems(query)
    with pytest.raises(ValueError, match="byte limit"):
        mcp_server.search_arxiv_theorems(query)


@pytest.mark.parametrize(
    ("search", "payload"),
    [
        (
            mcp_server.search_matlas_theorems,
            [{**MATLAS_RESULT, "statement": "x" * 40_000}],
        ),
        (
            mcp_server.search_arxiv_theorems,
            [
                {
                    "title": "Paper",
                    "theorem": "x" * 40_000,
                    "arxiv_id": "2601.01234",
                    "theorem_id": "thm-7",
                }
            ],
        ),
    ],
)
def test_search_success_envelope_is_bounded_without_text_truncation(
    monkeypatch: pytest.MonkeyPatch,
    search,
    payload: Any,
) -> None:
    monkeypatch.setattr(
        mcp_server.requests,
        "post",
        lambda *args, **kwargs: _Response(payload),
    )

    result = search("statement")

    assert result["retrieval_status"] == "error"
    assert result["error"] == "response_too_large"
    assert result["count"] == 0
    assert result["results"] == []


@pytest.mark.parametrize(
    ("search", "payload"),
    [
        (
            mcp_server.search_matlas_theorems,
            [{**MATLAS_RESULT, "statement": "\ud800"}],
        ),
        (
            mcp_server.search_arxiv_theorems,
            [
                {
                    "title": "Paper",
                    "theorem": "\ud800",
                    "arxiv_id": "2601.01234",
                    "theorem_id": "thm-7",
                }
            ],
        ),
    ],
)
def test_search_rejects_non_utf8_provider_text(
    monkeypatch: pytest.MonkeyPatch,
    search,
    payload: Any,
) -> None:
    monkeypatch.setattr(
        mcp_server.requests,
        "post",
        lambda *args, **kwargs: _Response(payload),
    )

    result = search("statement")

    assert result["retrieval_status"] == "error"
    assert result["error"] == "invalid_result:0:encoding"
    assert result["results"] == []


def test_official_matlas_rejects_nonconforming_nine_field_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    malformed = {**MATLAS_RESULT, "year": 2026}
    monkeypatch.setattr(
        mcp_server.requests,
        "post",
        lambda *args, **kwargs: _Response([malformed]),
    )

    result = mcp_server.search_matlas_theorems("statement")

    assert result["retrieval_status"] == "error"
    assert result["error"] == "invalid_result:0:fields"
    assert result["results"] == []
    assert result["count"] == 0


def test_official_matlas_outage_is_audited_without_legacy_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called_urls: list[str] = []

    def post(url: str, **kwargs: Any) -> _Response:
        called_urls.append(url)
        response = requests.Response()
        response.status_code = 521
        raise requests.HTTPError(response=response)

    monkeypatch.setattr(mcp_server.requests, "post", post)
    result = mcp_server.search_matlas_theorems("statement")

    assert called_urls == ["https://matlas.ai/api/search"]
    assert result["retrieval_status"] == "error"
    assert result["error"] == "http 521"
    assert result["provider"] == "matlas_official_v0_1"
    assert result["fallback_used"] is False
    assert result["results"] == []


@pytest.mark.parametrize(
    "search",
    [mcp_server.search_matlas_theorems, mcp_server.search_arxiv_theorems],
)
def test_provider_close_failure_after_success_is_audited(
    monkeypatch: pytest.MonkeyPatch,
    search,
) -> None:
    monkeypatch.setattr(
        mcp_server.requests,
        "post",
        lambda *args, **kwargs: _CloseFailingResponse([]),
    )

    result = search("statement")

    assert result["retrieval_status"] == "error"
    assert result["error"] == "network response_close_failed"
    assert result["results"] == []


@pytest.mark.parametrize(
    "search",
    [mcp_server.search_matlas_theorems, mcp_server.search_arxiv_theorems],
)
def test_provider_close_failure_does_not_override_http_error(
    monkeypatch: pytest.MonkeyPatch,
    search,
) -> None:
    monkeypatch.setattr(
        mcp_server.requests,
        "post",
        lambda *args, **kwargs: _CloseFailingResponse([], status_code=503),
    )

    result = search("statement")

    assert result["retrieval_status"] == "error"
    assert result["error"] == "http 503"
    assert result["results"] == []


@pytest.mark.parametrize(
    "search",
    [mcp_server.search_matlas_theorems, mcp_server.search_arxiv_theorems],
)
def test_provider_close_failure_does_not_override_stream_error(
    monkeypatch: pytest.MonkeyPatch,
    search,
) -> None:
    monkeypatch.setattr(
        mcp_server.requests,
        "post",
        lambda *args, **kwargs: _IterFailingCloseResponse(),
    )

    result = search("statement")

    assert result["retrieval_status"] == "error"
    assert result["error"] == "network ChunkedEncodingError"
    assert result["results"] == []


@pytest.mark.parametrize("announced_length", [True, False])
def test_legacy_raw_response_body_is_bounded_before_json_parse(
    monkeypatch: pytest.MonkeyPatch,
    announced_length: bool,
) -> None:
    cap = mcp_server.MAX_EXTERNAL_RAW_RESPONSE_BYTES
    response = _StreamingResponse(
        [] if announced_length else [b"x" * (cap // 2 + 1), b"x" * (cap // 2 + 1)],
        content_length=cap + 1 if announced_length else None,
    )
    monkeypatch.setattr(mcp_server.requests, "post", lambda *args, **kwargs: response)

    result = mcp_server.search_arxiv_theorems("statement")

    assert result["retrieval_status"] == "error"
    assert result["error"] == "response_too_large"
    assert result["results"] == []
    assert response.closed is True


def test_legacy_arxiv_provider_retains_distinct_four_field_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, Any], dict[str, str], int, bool]] = []
    upstream = {
        "title": "Legacy Paper",
        "theorem": "A legacy theorem statement.",
        "arxiv_id": "2601.01234",
        "theorem_id": "thm-7",
        "ignored_upstream_field": "drop",
    }

    def post(
        url: str,
        *,
        json: dict[str, Any],
        headers: dict[str, str],
        timeout: int,
        stream: bool,
    ) -> _Response:
        calls.append((url, json, headers, timeout, stream))
        return _Response([upstream])

    monkeypatch.setattr(mcp_server.requests, "post", post)
    result = mcp_server.search_arxiv_theorems(
        "  legacy statement  ",
        num_results=3,
        timeout_seconds=9,
    )

    assert calls == [
        (
            "https://leansearch.net/thm/search",
            {
                "query": "legacy statement",
                "task": mcp_server.THEOREM_SEARCH_TASK,
                "num_results": 3,
            },
            {
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "rethlas/1.0",
            },
            9,
            True,
        )
    ]
    assert result["schema_version"] == "rethlas_external_retrieval_v1"
    assert result["provider"] == "danus_legacy_arxiv_theorem_v1"
    assert result["provider_protocol"] == "danus_legacy_arxiv_theorem_search_v1"
    assert result["retrieval_status"] == "ok"
    assert result["fallback_used"] is False
    assert result["results"] == [
        {
            "title": "Legacy Paper",
            "theorem": "A legacy theorem statement.",
            "arxiv_id": "2601.01234",
            "theorem_id": "thm-7",
        }
    ]
    assert mcp_server.search_arxiv_theorems is not mcp_server.search_matlas_theorems


def test_legacy_arxiv_provider_truncates_an_overfull_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    upstream = [
        {
            "title": f"Paper {index}",
            "theorem": f"Statement {index}",
            "arxiv_id": f"2601.{index:05d}",
            "theorem_id": f"thm-{index}",
        }
        for index in range(5)
    ]
    monkeypatch.setattr(
        mcp_server.requests,
        "post",
        lambda *args, **kwargs: _Response(upstream),
    )

    result = mcp_server.search_arxiv_theorems("legacy statement", num_results=2)

    assert result["requested_count"] == 2
    assert result["count"] == 2
    assert result["results"] == upstream[:2]
