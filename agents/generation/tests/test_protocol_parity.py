from __future__ import annotations

import base64
import hashlib
import sys
import types
import zlib
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agents.generation.mcp import proof_context as generation_context  # noqa: E402
from agents import hotjoin_adapter  # noqa: E402
from agents.verification.api import proof_context as verification_context  # noqa: E402


def test_generation_and_verification_protocol_sources_are_identical() -> None:
    generation_source = Path(generation_context.__file__).read_text(encoding="utf-8")
    verification_source = Path(verification_context.__file__).read_text(encoding="utf-8")
    assert generation_source == verification_source


def test_embedded_continuous_supervisor_matches_production_source() -> None:
    source_path = REPO_ROOT / "agents" / "continuous_supervisor.py"
    source = source_path.read_bytes()
    embedded = zlib.decompress(
        base64.b85decode(
            "".join(hotjoin_adapter._CONTINUOUS_CONTRACT_ZLIB_B85.split())
        )
    )

    assert embedded == source
    assert hashlib.sha256(embedded).hexdigest() == (
        hotjoin_adapter._CONTINUOUS_CONTRACT_SHA256
    )

    fallback = types.ModuleType("_test_embedded_continuous_supervisor")
    exec(
        compile(embedded, "<embedded-continuous-supervisor>", "exec"),
        fallback.__dict__,
    )
    item_schema = fallback.COHORT_REPORT_JSON_SCHEMA["properties"][
        "lane_decisions"
    ]["items"]
    assert item_schema["properties"]["next_test"] == {
        "type": "string",
        "minLength": 1,
    }
    assert fallback.NO_NEXT_TEST == "NO_NEXT_TEST"

    summary = {
        "lane_id": "lane-a",
        "thread_id": "thread-a",
        "route_id": "route-a",
        "candidate_status": "none",
        "proved_claim_evidence_ids": [],
        "failed_path_evidence_ids": [],
        "counterexample_evidence_ids": [],
    }
    missing_redirect_test = {
        "lane_id": "lane-a",
        "thread_id": "thread-a",
        "route_id": "route-a",
        "verdict": "red",
        "next_action": "redirect",
        "redirect_to_lane_id": "lane-b",
        "reason": "The source route is exhausted.",
        "fatal_doubt": None,
        "next_test": None,
        "accepted_evidence_ids": [],
    }
    with pytest.raises(
        fallback.ContinuousSupervisorError,
        match="concrete next test",
    ):
        fallback.validate_lane_decision(missing_redirect_test, summary=summary)


def test_generation_independently_recomputes_manifest_and_context_attestation() -> None:
    proof = """# lemma lem:a

<!-- rethlas-depends-on: -->
## statement
A
## proof
Proof of A.

# lemma lem:b

<!-- rethlas-depends-on: -->
## statement
B
## proof
Proof of B.

# theorem thm:main

## statement
S
## proof
Use lem:a and lem:b to prove S.
"""

    generation_manifest = generation_context.parse_blueprint(
        proof,
        target_statement="S",
    )
    verification_manifest = verification_context.parse_blueprint(
        proof,
        target_statement="S",
    )

    assert generation_manifest.item_ids == verification_manifest.item_ids
    assert generation_context.aggregate_context_digest(
        generation_manifest
    ) == verification_context.aggregate_context_digest(verification_manifest)
    for item_id in generation_manifest.item_ids:
        assert generation_context.build_item_context(
            generation_manifest,
            item_id,
            max_chars=200_000,
        ) == verification_context.build_item_context(
            verification_manifest,
            item_id,
            max_chars=200_000,
        )

    generation_attestations = []
    verification_attestations = []
    root_id = generation_manifest.item_ids[0]
    for index, item_id in enumerate(generation_manifest.item_ids):
        expanded_ids = [root_id] if index == len(generation_manifest.item_ids) - 1 else []
        round_index = 1 if expanded_ids else 0
        generation_item_context = generation_context.build_item_context(
            generation_manifest,
            item_id,
            max_chars=200_000,
            expanded_proof_ids=expanded_ids,
            round_index=round_index,
        )
        verification_item_context = verification_context.build_item_context(
            verification_manifest,
            item_id,
            max_chars=200_000,
            expanded_proof_ids=expanded_ids,
            round_index=round_index,
        )
        for records, context in (
            (generation_attestations, generation_item_context),
            (verification_attestations, verification_item_context),
        ):
            records.append(
                {
                    "item_id": item_id,
                    "disposition": "verified",
                    "final_round": round_index,
                    "expanded_proof_ids": expanded_ids,
                    "max_chars": 200_000,
                    "context_digest": context["digest"],
                    "verdict": "correct",
                }
            )

    assert generation_context.aggregate_adaptive_context_digest(
        generation_manifest, generation_attestations
    ) == verification_context.aggregate_adaptive_context_digest(
        verification_manifest, verification_attestations
    )
