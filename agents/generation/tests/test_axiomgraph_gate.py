"""Graph-native IDs may never fall through to legacy trusted-state producers."""

from pathlib import Path
import hashlib

import pytest

from agents import advisor_bridge, claude_core, hotjoin_adapter
from agents.generation.mcp import server, verification_client


GRAPH_PROBLEM = "axiomgraph:gr1_" + "a" * 64


@pytest.mark.parametrize("value", [GRAPH_PROBLEM, "./" + GRAPH_PROBLEM, "prefix/" + GRAPH_PROBLEM, " " + GRAPH_PROBLEM + " "])
def test_memory_normalization_never_creates_a_legacy_alias(value):
    with pytest.raises(ValueError, match="AxiomGraph source gate"):
        server.sanitize_problem_id(value)


def test_existing_controller_review_and_advisor_id_contracts_reject_graph_uris():
    with pytest.raises(claude_core.ClaudeCoreError):
        claude_core._safe_problem_id(GRAPH_PROBLEM)
    with pytest.raises(ValueError):
        advisor_bridge._validate_problem_id(GRAPH_PROBLEM)
    assert hotjoin_adapter._valid_memory_batch_problem_id(GRAPH_PROBLEM) is False
    with pytest.raises(ValueError):
        server.validate_verified_problem_id(GRAPH_PROBLEM)


@pytest.mark.parametrize("via_path", [False, True])
@pytest.mark.parametrize("prefix", ["", " ", "prefix/", "prefix\\\\"])
def test_legacy_verifier_rejects_before_reading_files_or_dispatch(via_path, prefix, tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("legacy verifier made an external effect")
    monkeypatch.setattr(verification_client.requests, "post", forbidden)
    identity = prefix + GRAPH_PROBLEM
    directory = tmp_path / identity if via_path else tmp_path
    with pytest.raises(ValueError, match="AxiomGraph source admission gate"):
        verification_client.verify_blueprint_file(statement="test", draft_path=directory / "draft.md",
                                                   verified_path=directory / "verified.md", endpoint="http://never.invalid",
                                                   problem_id=None if via_path else identity)
    assert list(tmp_path.iterdir()) == []


def test_no_runner_enables_graph_native_work_via_legacy_path():
    root = Path(__file__).parent
    for name in ("run_legacy.sh", "run_hotjoin.sh", "run_claude_core.sh"):
        text = (root / name).read_text()
        assert '== *axiomgraph:*' in text
        assert "Graph-native identities require the AxiomGraph source gate" in text


def test_current_runtime_dependency_closure_includes_the_rebuilt_legacy_guards():
    for relative, expected in claude_core.RUNTIME_DEPENDENCY_SHA256.items():
        assert hashlib.sha256((claude_core.GENERATION_ROOT / relative).read_bytes()).hexdigest() == expected, relative
