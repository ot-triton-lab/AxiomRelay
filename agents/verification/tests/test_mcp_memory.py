from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


MODULE_PATH = Path(__file__).resolve().parents[1] / "mcp" / "server.py"
SPEC = importlib.util.spec_from_file_location("rethlas_verification_mcp", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
mcp_server = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mcp_server)


@pytest.fixture()
def isolated_memory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "memory"
    monkeypatch.setattr(mcp_server, "MEMORY_ROOT", root)
    return root


def test_memory_append_returns_metadata_without_echoing_record(
    isolated_memory: Path,
) -> None:
    response = mcp_server.memory_append(
        "run-1",
        "statement_checks",
        {"large_proof_fragment": "x" * 10_000},
    )

    assert response["status"] == "ok"
    assert len(response["record_id"]) == 16
    assert "entry" not in response
    assert "large_proof_fragment" not in str(response)


def test_memory_query_is_newest_first_and_reports_budget_truncation(
    isolated_memory: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timestamps = iter(range(100))
    monkeypatch.setattr(
        mcp_server,
        "_utc_now",
        lambda: f"2026-01-01T00:00:{next(timestamps):02d}+00:00",
    )
    mcp_server.memory_append("run-1", "statement_checks", {"text": "older match " + "a" * 40})
    mcp_server.memory_append("run-1", "statement_checks", {"text": "newer match " + "b" * 40})

    all_results = mcp_server.memory_query(
        "run-1",
        "statement_checks",
        contains="match",
        max_chars=10_000,
    )
    assert all_results["complete"] is True
    assert all_results["items"][0]["record"]["text"].startswith("newer")

    newest_chars = len(mcp_server._canonical_json(all_results["items"][0]))
    limited = mcp_server.memory_query(
        "run-1",
        "statement_checks",
        contains="match",
        max_chars=newest_chars,
    )
    assert limited["count"] == 1
    assert limited["corpus_count"] == 2
    assert limited["complete"] is False
    assert limited["truncated"] is True
    assert limited["omitted_count"] == 1
    assert len(limited["omitted_ids"]) == 1
    assert limited["returned_chars"] == newest_chars


def test_memory_query_rejects_non_positive_budget(isolated_memory: Path) -> None:
    with pytest.raises(ValueError, match="max_chars"):
        mcp_server.memory_query("run-1", "events", max_chars=0)


def test_production_mcp_surface_has_no_verdict_validation_or_write_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeFastMCP:
        def __init__(self, name: str) -> None:
            self.name = name
            self.tool_names: list[str] = []

        def tool(self, *, name: str):  # type: ignore[no-untyped-def]
            def decorate(function):  # type: ignore[no-untyped-def]
                self.tool_names.append(name)
                return function

            return decorate

    monkeypatch.setattr(mcp_server, "FastMCP", FakeFastMCP)
    app = mcp_server.build_mcp_app()

    assert app is not None
    assert set(app.tool_names) == {
        "search_matlas_theorems",
        "search_arxiv_theorems",
        "memory_init",
        "memory_append",
        "memory_query",
    }
    assert "validate_verification_output" not in app.tool_names
    assert "write_verification_output" not in app.tool_names
