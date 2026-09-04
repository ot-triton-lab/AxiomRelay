from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "agents"))
sys.path.insert(0, str(REPO / "agents" / "generation" / "mcp"))
SIBLING_AXIOMGRAPH_SRC = REPO.parent / "AxiomGraph" / "src"
if SIBLING_AXIOMGRAPH_SRC.is_dir():
    sys.path.insert(0, str(SIBLING_AXIOMGRAPH_SRC))

import axiomgraph_contract as ag
import publication_proof_context_v3 as proof_context
from axiomgraph_shadow_adapter import (
    AxiomGraphProjectionError,
    project_reconciled_publication_v6,
    projection_document,
    write_publication_projection,
)


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _canonical(value) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


class PublicationShadowTests(unittest.TestCase):
    def inputs(self):
        statement = b"T\n"
        blueprint = (
            "# lemma lem:base\n\n"
            "<!-- rethlas-depends-on: -->\n"
            "## statement\nP\n\n"
            "## proof\nA verified base proof.\n\n"
            "# theorem thm:target\n\n"
            "<!-- rethlas-depends-on: lem:base -->\n"
            "## statement\nT\n\n"
            "## proof\nThe verified bridge from P.\n"
        ).encode("utf-8")
        manifest = proof_context.parse_blueprint(
            blueprint.decode("utf-8"), target_statement=statement.decode("utf-8")
        )
        receipt = {
            "canonical_target_digest": _sha256(b"T"),
            "checked_item_ids": list(manifest.item_ids),
            "proof_context": {
                "adaptive_aggregate_context_schema_version": 2,
                "aggregate_context_schema_version": 1,
                "proof_context_schema_version": 2,
                "proof_item_schema_version": 1,
                "schema_version": "rethlas_publication_proof_context_binding_v1",
                "source_sha256": "1" * 64,
            },
            "proof_digest": _sha256(blueprint),
            "schema_version": "rethlas-publication-v6",
            "state": "active",
            "statement_source_digest": _sha256(statement),
            "verification_limits": {
                "context_max_chars": 100_000,
                "max_blueprint_bytes": 100_000,
                "max_blueprint_chars": 100_000,
                "max_expanded_proof_chars": 100_000,
                "max_expanded_proofs": 3,
                "max_expansion_rounds": 2,
                "max_proof_items": 10,
                "max_receipt_bytes": 100_000,
            },
            "verification_passes": [
                {
                    "pass_index": 1,
                    "verdict": "correct",
                    "verification_role": "primary",
                    "verifier_model": "verifier-a",
                    "verifier_reasoning_effort": "high",
                    "verifier_service_version": "test-v1",
                },
                {
                    "pass_index": 2,
                    "verdict": "correct",
                    "verification_role": "adversarial_full_claim_audit",
                    "verifier_model": "verifier-b",
                    "verifier_reasoning_effort": "high",
                    "verifier_service_version": "test-v1",
                },
            ],
            "verification_quorum": 2,
        }
        receipt_sha256 = _sha256(_canonical(receipt) + b"\n")
        return statement, blueprint, manifest, receipt, receipt_sha256

    def project(self):
        statement, blueprint, manifest, receipt, receipt_sha256 = self.inputs()
        projection = project_reconciled_publication_v6(
            problem_id="test/problem",
            statement_raw=statement,
            blueprint_raw=blueprint,
            receipt=receipt,
            publication_receipt_sha256=receipt_sha256,
            proof_context_parser=proof_context,
        )
        return projection, statement, blueprint, manifest, receipt

    def test_projection_preserves_source_ids_and_builds_grounded_target_cone(self) -> None:
        projection, statement, _blueprint, manifest, _receipt = self.project()
        self.assertTrue(projection.validation.grounded)
        self.assertTrue(projection.validation.publication_eligible)
        self.assertIsNotNone(projection.validation.authentication_id)
        self.assertEqual(projection.bundle.exact_target_blob.content_bytes(), statement)
        self.assertTrue(projection.bundle.environment_blobs)
        self.assertEqual(len(projection.bundle.derivation_snapshot.derivations), len(manifest.items))
        self.assertEqual(
            {item.proof_artifact_digest for item in projection.bundle.derivation_snapshot.derivations},
            {"sha256:" + item.digest for item in manifest.items},
        )
        self.assertEqual(projection.selection.target_statement_id, projection.bundle.target_statement_id)

        raw_validation = ag.validate_selection(
            projection.bundle.derivation_snapshot,
            projection.selection,
        )
        self.assertTrue(raw_validation.grounded)
        self.assertFalse(raw_validation.publication_eligible)
        self.assertIn("SNAPSHOT_NOT_AUTHENTICATED", raw_validation.publication_blockers)

    def test_distinct_proof_items_that_quotient_to_one_statement_are_one_premise(self) -> None:
        statement = b"T\n"
        blueprint = (
            "# lemma lem:first\n\n"
            "<!-- rethlas-depends-on: -->\n"
            "## statement\nP\n\n"
            "## proof\nFirst verified proof.\n\n"
            "# lemma lem:second\n\n"
            "<!-- rethlas-depends-on: -->\n"
            "## statement\nP\n\n"
            "## proof\nSecond verified proof.\n\n"
            "# theorem thm:target\n\n"
            "<!-- rethlas-depends-on: lem:first, lem:second -->\n"
            "## statement\nT\n\n"
            "## proof\nThe verified bridge from either proof of P.\n"
        ).encode("utf-8")
        manifest = proof_context.parse_blueprint(
            blueprint.decode("utf-8"), target_statement=statement.decode("utf-8")
        )
        _old_statement, _old_blueprint, _old_manifest, receipt, _old_digest = self.inputs()
        receipt["checked_item_ids"] = list(manifest.item_ids)
        receipt["proof_digest"] = _sha256(blueprint)
        receipt_sha256 = _sha256(_canonical(receipt) + b"\n")

        projection = project_reconciled_publication_v6(
            problem_id="test/quotient",
            statement_raw=statement,
            blueprint_raw=blueprint,
            receipt=receipt,
            publication_receipt_sha256=receipt_sha256,
            proof_context_parser=proof_context,
        )

        selected = dict(projection.selection.by_conclusion)
        target_derivation_id = selected[projection.bundle.target_statement_id]
        target_derivation = next(
            item
            for item in projection.bundle.derivation_snapshot.derivations
            if item.derivation_id == target_derivation_id
        )
        self.assertEqual(len(target_derivation.premise_ids), 1)
        self.assertTrue(projection.validation.publication_eligible)

    def test_projection_is_write_once_and_byte_deterministic(self) -> None:
        projection, *_ = self.project()
        expected = projection_document(projection)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "shadow"
            first = write_publication_projection(projection=projection, shadow_root=root)
            second = write_publication_projection(projection=projection, shadow_root=root)
            self.assertEqual(first, second)
            self.assertEqual(Path(first["path"]).read_bytes(), expected)
            self.assertEqual(Path(first["path"]).stat().st_mode & 0o777, 0o400)

            document = json.loads(expected)
            nested_bundle = ag.canonical_bytes(document["bundle_document"])
            self.assertEqual(ag.load_graph_bundle(nested_bundle), projection.bundle)

    def test_tampered_source_or_receipt_is_rejected(self) -> None:
        statement, blueprint, _manifest, receipt, receipt_sha256 = self.inputs()
        with self.assertRaises(AxiomGraphProjectionError):
            project_reconciled_publication_v6(
                problem_id="test/problem",
                statement_raw=statement + b"changed",
                blueprint_raw=blueprint,
                receipt=receipt,
                publication_receipt_sha256=receipt_sha256,
                proof_context_parser=proof_context,
            )
        with self.assertRaises(AxiomGraphProjectionError):
            project_reconciled_publication_v6(
                problem_id="test/problem",
                statement_raw=statement,
                blueprint_raw=blueprint,
                receipt=receipt,
                publication_receipt_sha256="0" * 64,
                proof_context_parser=proof_context,
            )


if __name__ == "__main__":
    unittest.main()
