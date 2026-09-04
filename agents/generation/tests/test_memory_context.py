from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import types
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from unittest import mock


try:
    import requests  # noqa: F401
except ModuleNotFoundError:  # Keep these memory-only tests dependency-free.
    sys.modules["requests"] = types.SimpleNamespace(post=None)

from agents.generation.mcp import server


LARGE_SEARCH_BUDGET = 1_000_000


class MemoryContextTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self._original_memory_root = server.MEMORY_ROOT
        server.MEMORY_ROOT = Path(self._temporary_directory.name)

    def tearDown(self) -> None:
        server.MEMORY_ROOT = self._original_memory_root
        self._temporary_directory.cleanup()

    def _continuous_round_records(
        self, problem_id: str, *, mismatch: bool = False
    ) -> tuple[list[str], str]:
        reports = []
        for index in range(3):
            text = f"bounded report for plan {index}"
            reports.append(
                {
                    "channel": "failed_paths",
                    "record": {
                        "schema_version": server.ROUTE_TERMINAL_REPORT_SCHEMA,
                        "thread_id": f"thread-{index}",
                        "plan_id": f"plan-{index}",
                        "status": "blocked",
                        "report_text": text,
                        "report_sha256": hashlib.sha256(text.encode()).hexdigest(),
                        "remaining_obligations": [f"obligation {index}"],
                        "decisive_stuck_points": [f"stuck point {index}"],
                    },
                }
            )
        report_receipt = server.memory_append_batch(problem_id, reports)
        report_ids = [item["record_id"] for item in report_receipt["records"]]
        synthesis_receipt = server.memory_append_batch(
            problem_id,
            [
                {
                    "channel": "failed_paths",
                    "record": {
                        "schema_version": server.ROUND_FAILURE_SYNTHESIS_SCHEMA,
                        "record_type": "key_failures_summary",
                        "route_report_record_ids": (
                            report_ids[:2] if mismatch else report_ids
                        ),
                        "failed_plan_ids": [f"plan-{index}" for index in range(3)],
                        "plan_failures": [
                            {
                                "plan_id": f"plan-{index}",
                                "stuck_points": [f"stuck point {index}"],
                            }
                            for index in range(3)
                        ],
                        "common_failures": ["one shared obstruction"],
                        "implications_for_next_plans": [
                            "use three genuinely different mechanisms"
                        ],
                        "next_state": "new_cohort",
                    },
                }
            ],
        )
        return report_ids, synthesis_receipt["records"][0]["record_id"]

    def test_continuous_round_finish_validates_active_memory_before_host_call(
        self,
    ) -> None:
        problem_id = "continuous/round-finish"
        report_ids, synthesis_id = self._continuous_round_records(problem_id)
        captured: dict[str, object] = {}

        def admit(**kwargs: object) -> dict[str, object]:
            captured.update(kwargs)
            return {"state": "host-finished"}

        with (
            mock.patch.dict(
                os.environ,
                {
                    "RETHLAS_EXPECTED_PROBLEM_ID": problem_id,
                    server._REVIEW_RUN_ENV: "run-round-finish",
                },
                clear=False,
            ),
            mock.patch.object(
                server, "_adapter_continuous_round_finish", side_effect=admit
            ),
            mock.patch.object(server, "_released_memory_registry_mode", return_value=None),
        ):
            result = server.continuous_round_finish(
                problem_id, report_ids, synthesis_id
            )

        self.assertEqual(result, {"state": "host-finished"})
        self.assertEqual(captured["next_action"], "new_cohort")
        normalized_reports = captured["reports"]
        self.assertEqual(len(normalized_reports), 3)
        self.assertEqual(
            {item["thread_id"] for item in normalized_reports},
            {"thread-0", "thread-1", "thread-2"},
        )
        self.assertEqual(
            {item["report_sha256"] for item in normalized_reports},
            {
                hashlib.sha256(
                    f"bounded report for plan {index}".encode()
                ).hexdigest()
                for index in range(3)
            },
        )
        evidence = {
            "reports": [
                {
                    key: value
                    for key, value in item.items()
                    if key != "report_sha256"
                }
                for item in captured["reports"]
            ],
            "synthesis": captured["synthesis"],
            "next_action": captured["next_action"],
        }
        self.assertEqual(
            captured["evidence_sha256"],
            hashlib.sha256(server.canonical_json_bytes(evidence)).hexdigest(),
        )

    def test_continuous_round_status_binds_active_problem_before_host_call(
        self,
    ) -> None:
        problem_id = "continuous/round-status"
        expected = {"state": "ready", "lanes": [{"thread_id": "thread-0"}]}
        with (
            mock.patch.dict(
                os.environ,
                {"RETHLAS_EXPECTED_PROBLEM_ID": problem_id},
                clear=False,
            ),
            mock.patch.object(
                server,
                "_adapter_continuous_round_status",
                return_value=expected,
            ) as status,
        ):
            self.assertEqual(server.continuous_round_status(problem_id), expected)
        status.assert_called_once_with(problem_id=problem_id)

    def test_memory_batch_rejects_malformed_route_report_before_write(self) -> None:
        problem_id = "continuous/malformed-route-report"
        report_text = "bounded terminal source"
        with self.assertRaisesRegex(ValueError, "exact released schema"):
            server.memory_append_batch(
                problem_id,
                [
                    {
                        "channel": "proof_steps",
                        "record": {
                            "schema_version": server.ROUTE_TERMINAL_REPORT_SCHEMA,
                            "thread_id": "thread-a",
                            "plan_id": "plan-a",
                            "status": "partial",
                            "report_text": report_text,
                            "report_sha256": hashlib.sha256(
                                report_text.encode()
                            ).hexdigest(),
                            "remaining_obligations": ["one obligation"],
                            "decisive_stuck_points": ["one obstruction"],
                            "manifest_sha256": "0" * 64,
                        },
                    }
                ],
            )
        self.assertFalse(server._batch_checkpoint_dir(problem_id).exists())

    def test_route_terminal_helper_builds_exact_schema_for_real_report_size(
        self,
    ) -> None:
        problem_id = "legacy/route-terminal-helper"
        report_text = "x" * 9_140

        receipt = server.append_route_terminal_report(
            problem_id=problem_id,
            thread_id="thread-a",
            plan_id="route-a",
            status="partial",
            report_text=f"  {report_text}\n",
            remaining_obligations=["close the converse"],
            decisive_stuck_points=["the cited theorem was not yet readable"],
        )

        self.assertEqual(receipt["status"], "ok")
        self.assertEqual(receipt["count"], 1)
        logical = server._load_memory_entries(problem_id)
        self.assertEqual(len(logical["proof_steps"]), 1)
        record = logical["proof_steps"][0]["item"]["record"]
        self.assertEqual(
            set(record),
            {
                "schema_version",
                "thread_id",
                "plan_id",
                "status",
                "report_text",
                "report_sha256",
                "remaining_obligations",
                "decisive_stuck_points",
            },
        )
        self.assertEqual(record["schema_version"], server.ROUTE_TERMINAL_REPORT_SCHEMA)
        self.assertEqual(record["report_text"], report_text)
        self.assertEqual(
            record["report_sha256"],
            hashlib.sha256(report_text.encode()).hexdigest(),
        )

    def test_route_terminal_helper_reports_exact_overflow_before_write(self) -> None:
        problem_id = "legacy/route-terminal-overflow"
        report_text = "界" * 5_462
        byte_count = len(report_text.encode("utf-8"))
        self.assertEqual(byte_count, server.MAX_ROUTE_TERMINAL_REPORT_BYTES + 2)

        with self.assertRaisesRegex(
            server.MemoryCheckpointPreflightError,
            rf"{byte_count} UTF-8 bytes.*{server.MAX_ROUTE_TERMINAL_REPORT_BYTES}",
        ) as raised:
            server.append_route_terminal_report(
                problem_id=problem_id,
                thread_id="thread-a",
                plan_id="route-a",
                status="blocked",
                report_text=report_text,
                remaining_obligations=["compress the report"],
                decisive_stuck_points=["hard byte limit"],
            )

        self.assertTrue(raised.exception.retry_allowed)
        self.assertFalse(server._batch_checkpoint_dir(problem_id).exists())

    def test_route_terminal_helper_does_not_mislabel_post_preflight_failure(
        self,
    ) -> None:
        with mock.patch.object(
            server,
            "memory_append_batch",
            side_effect=ValueError("post-preflight durability failure"),
        ):
            with self.assertRaisesRegex(
                ValueError, "post-preflight durability failure"
            ) as raised:
                server.append_route_terminal_report(
                    problem_id="legacy/route-terminal-post-preflight",
                    thread_id="thread-a",
                    plan_id="route-a",
                    status="blocked",
                    report_text="bounded report",
                    remaining_obligations=["retry only after diagnosis"],
                    decisive_stuck_points=["durability failure"],
                )

        self.assertNotIsInstance(
            raised.exception,
            server.MemoryCheckpointPreflightError,
        )

    def test_continuous_round_finish_rejects_incomplete_synthesis_before_host(
        self,
    ) -> None:
        problem_id = "continuous/round-finish-mismatch"
        report_ids, synthesis_id = self._continuous_round_records(
            problem_id, mismatch=True
        )
        with (
            mock.patch.dict(
                os.environ,
                {"RETHLAS_EXPECTED_PROBLEM_ID": problem_id},
                clear=False,
            ),
            mock.patch.object(server, "_adapter_continuous_round_finish") as admit,
            mock.patch.object(server, "_released_memory_registry_mode", return_value=None),
            self.assertRaisesRegex(ValueError, "synthesis"),
        ):
            server.continuous_round_finish(problem_id, report_ids, synthesis_id)
        admit.assert_not_called()

    def test_memory_append_returns_compact_receipt_by_default(self) -> None:
        receipt = server.memory_append(
            "sample/problem",
            "proof_steps",
            {"claim": "the full record should not be echoed"},
        )

        self.assertEqual(receipt["status"], "ok")
        self.assertEqual(receipt["problem_id"], "sample/problem")
        self.assertEqual(receipt["channel"], "proof_steps")
        self.assertTrue(receipt["record_id"].startswith("mem_"))
        self.assertTrue(receipt["active"])
        self.assertEqual(receipt["supersedes"], [])
        self.assertNotIn("entry", receipt)
        self.assertEqual(
            receipt["path"],
            str(server._channel_path("sample/problem", "proof_steps")),
        )

        stored = list(
            server._iter_jsonl(server._channel_path("sample/problem", "proof_steps"))
        )
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]["record_id"], receipt["record_id"])
        self.assertEqual(
            stored[0]["record"], {"claim": "the full record should not be echoed"}
        )

        event = list(
            server._iter_jsonl(server._channel_path("sample/problem", "events"))
        )[0]
        self.assertEqual(event["appended_record_id"], receipt["record_id"])

    def test_memory_append_full_mode_preserves_legacy_response_payload(self) -> None:
        receipt = server.memory_append(
            "sample",
            "toy_examples",
            {"example": "expanded response"},
            return_mode="full",
        )

        self.assertIn("path", receipt)
        self.assertIn("entry", receipt)
        self.assertEqual(receipt["entry"]["record_id"], receipt["record_id"])
        self.assertEqual(receipt["entry"]["record"], {"example": "expanded response"})

    def test_released_legacy_jsonl_writes_fail_before_any_filesystem_write(
        self,
    ) -> None:
        with mock.patch.object(
            server, "_released_memory_registry_configured", return_value=True
        ):
            with self.assertRaisesRegex(
                ValueError, "memory_append is offline-only"
            ):
                server.memory_append(
                    "released/problem",
                    "events",
                    {"event_type": "must-not-be-published"},
                )
            with self.assertRaisesRegex(
                ValueError, "memory_append is offline-only"
            ):
                server.branch_update(
                    "released/problem",
                    "root",
                    {"status": "must-not-be-published"},
                )

        self.assertFalse((server.MEMORY_ROOT / "released").exists())

    def test_offline_branch_update_remains_legacy_compatible(self) -> None:
        receipt = server.branch_update(
            "offline/problem",
            "root",
            {"status": "active"},
        )

        self.assertEqual(receipt["status"], "ok")
        self.assertEqual(receipt["channel"], "branch_states")
        stored = list(
            server._iter_jsonl(
                server._channel_path("offline/problem", "branch_states")
            )
        )
        self.assertEqual(stored[0]["record"]["branch_id"], "root")
        self.assertEqual(stored[0]["record"]["state"], {"status": "active"})

    def test_memory_append_batch_persists_one_compact_phase_checkpoint(self) -> None:
        response = server.memory_append_batch(
            "sample/problem",
            [
                {
                    "channel": "proof_steps",
                    "record": {"claim": "frontier-changing lemma"},
                },
                {
                    "channel": "failed_paths",
                    "record": {"obstruction": "decisive counterexample"},
                    "active": False,
                    "supersedes": ["mem_old"],
                },
            ],
        )

        self.assertEqual(response["status"], "ok")
        self.assertEqual(
            response["schema_version"],
            server.MEMORY_BATCH_LOCAL_COMMIT_RECEIPT_SCHEMA,
        )
        self.assertEqual(
            set(response),
            {
                "schema_version",
                "status",
                "problem_id",
                "batch_id",
                "checkpoint_sha256",
                "timestamp_utc",
                "committed_at_utc",
                "committed_at_monotonic",
                "commit_sha256",
                "count",
                "records",
                "checkpoint_path",
            },
        )
        self.assertNotIn("publication_receipt", response)
        self.assertEqual(response["problem_id"], "sample/problem")
        self.assertEqual(response["count"], 2)
        self.assertTrue(response["batch_id"].startswith("batch_"))
        self.assertNotIn("entry", response)
        self.assertNotIn("record", response["records"][0])

        logical = server._load_memory_entries("sample/problem")
        proof = [entry["item"] for entry in logical["proof_steps"]]
        failed = [entry["item"] for entry in logical["failed_paths"]]
        self.assertEqual(proof[0]["batch_id"], response["batch_id"])
        self.assertEqual(proof[0]["record"], {"claim": "frontier-changing lemma"})
        self.assertEqual(failed[0]["batch_id"], response["batch_id"])
        self.assertFalse(failed[0]["active"])
        self.assertEqual(failed[0]["supersedes"], ["mem_old"])

        events = [entry["item"] for entry in logical["events"]]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event_type"], "memory_append_batch")
        self.assertEqual(events[0]["batch_id"], response["batch_id"])
        self.assertEqual(len(events[0]["appended_records"]), 2)
        self.assertEqual(
            list(
                server._iter_jsonl(
                    server._channel_path("sample/problem", "proof_steps")
                )
            ),
            [],
        )
        checkpoint = Path(response["checkpoint_path"])
        self.assertTrue(checkpoint.is_file())
        checkpoint_payload = json.loads(checkpoint.read_text(encoding="utf-8"))
        self.assertEqual(checkpoint_payload["schema"], "rethlas_memory_batch_v3")
        self.assertRegex(checkpoint_payload["checkpoint_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(
            [
                path.name
                for path in checkpoint.parent.glob("batch_*.json")
                if not path.name.endswith(".commit.json")
            ],
            [f"{response['batch_id']}.json"],
        )
        marker = server._batch_commit_path(checkpoint)
        self.assertTrue(marker.is_file())
        self.assertEqual(
            response["committed_at_utc"],
            json.loads(marker.read_text())["committed_at_utc"],
        )

    def test_legacy_runner_common_env_gets_exact_durable_local_receipt(
        self,
    ) -> None:
        legacy_common_env = {
            "PYTHONDONTWRITEBYTECODE": "1",
            "RETHLAS_GENERATION_CONTROL_TOKEN": "1" * 32,
            "RETHLAS_EXPECTED_PROBLEM_ID": "legacy/common-env",
            "RETHLAS_EXPECTED_STATEMENT_SHA256": "2" * 64,
            "RETHLAS_GENERATION_ROOT": "/legacy/generation",
            "RETHLAS_RECEIPTS_ROOT": "/legacy/receipts",
            # This runtime attestation is intentionally common to released and
            # legacy runs, so it must never be treated as a release sentinel.
            "RETHLAS_TRUSTED_RUNTIME_SHA256": "3" * 64,
            "VERIFY_PROOF_URL": "http://127.0.0.1:8091/verify",
        }
        items = [
            {
                "channel": "proof_steps",
                "record": {"claim": "legacy local commit witness"},
            }
        ]
        real_revalidate = server._durably_revalidate_memory_batch_artifacts
        with (
            mock.patch.dict(os.environ, legacy_common_env, clear=True),
            mock.patch.object(
                server,
                "_durably_revalidate_memory_batch_artifacts",
                wraps=real_revalidate,
            ) as revalidate,
        ):
            self.assertFalse(server._released_memory_registry_configured())
            self.assertIsNone(
                server._reasoning_phase_preflight("memory_append_batch")
            )
            first = server.memory_append_batch("legacy/common-env", items)
            replay = server.memory_append_batch("legacy/common-env", items)

        self.assertEqual(replay, first)
        self.assertEqual(revalidate.call_count, 2)
        self.assertEqual(
            first["schema_version"],
            server.MEMORY_BATCH_LOCAL_COMMIT_RECEIPT_SCHEMA,
        )
        self.assertNotIn("publication_receipt", first)
        checkpoint = Path(first["checkpoint_path"])
        marker = server._batch_commit_path(checkpoint)
        self.assertTrue(checkpoint.is_file())
        self.assertTrue(marker.is_file())
        marker_payload = json.loads(marker.read_text(encoding="utf-8"))
        self.assertEqual(marker_payload["batch_id"], first["batch_id"])
        self.assertEqual(
            marker_payload["checkpoint_sha256"], first["checkpoint_sha256"]
        )
        self.assertEqual(marker_payload["commit_sha256"], first["commit_sha256"])

    def test_any_released_env_sentinel_without_registry_fails_closed(self) -> None:
        problem_id = "released-sentinel-no-registry"
        items = [
            {
                "channel": "proof_steps",
                "record": {"claim": "must not become a local receipt"},
            }
        ]
        checkpoint_dir = server._batch_checkpoint_dir(problem_id)
        for sentinel in sorted(server._RELEASED_REASONING_ENV_SENTINELS):
            for value in ("released-binding", ""):
                with self.subTest(sentinel=sentinel, value=value):
                    with mock.patch.dict(
                        os.environ, {sentinel: value}, clear=True
                    ):
                        with self.assertRaisesRegex(
                            ValueError, "released reasoning registry is incomplete"
                        ):
                            server._released_memory_registry_configured()
                        with self.assertRaisesRegex(
                            ValueError, "released reasoning registry is incomplete"
                        ):
                            server._reasoning_phase_preflight(
                                "memory_append_batch"
                            )
                        with self.assertRaisesRegex(
                            ValueError, "released reasoning registry is incomplete"
                        ):
                            server.memory_append_batch(problem_id, items)
                        with self.assertRaisesRegex(
                            ValueError, "released reasoning registry is incomplete"
                        ):
                            server.memory_append(
                                problem_id,
                                "not-a-channel",
                                [],  # type: ignore[arg-type]
                            )
                        with self.assertRaisesRegex(
                            ValueError, "released reasoning registry is incomplete"
                        ):
                            server.branch_update(problem_id, "root", {})
                    self.assertFalse(checkpoint_dir.exists())
                    self.assertFalse((server.MEMORY_ROOT / problem_id).exists())

    def test_complete_nonempty_released_registry_is_required_as_one_tuple(
        self,
    ) -> None:
        complete = {
            name: f"bound-{index}"
            for index, name in enumerate(
                server._RELEASED_MEMORY_REGISTRY_ENV_NAMES, start=1
            )
        }
        with mock.patch.dict(os.environ, complete, clear=True):
            self.assertTrue(server._released_memory_registry_configured())

        for missing in server._RELEASED_MEMORY_REGISTRY_ENV_NAMES:
            partial = dict(complete)
            partial.pop(missing)
            with self.subTest(missing=missing):
                with mock.patch.dict(os.environ, partial, clear=True):
                    with self.assertRaisesRegex(
                        ValueError, "released reasoning registry is incomplete"
                    ):
                        server._released_memory_registry_configured()

    def test_owner_manifest_snapshot_is_explicit_read_only_and_token_disjoint(
        self,
    ) -> None:
        problem_id = "frontier/example"
        run_id = "run-snapshot"
        snapshot = json.dumps(
            {
                "schema_version": "rethlas_memory_batch_publication_status_v1",
                "run_id": run_id,
                "problem_id": problem_id,
                "receipts": [],
            },
            allow_nan=False,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        static = {
            "RETHLAS_REVIEW_ADAPTER_PATH": "/trusted/hotjoin_adapter.py",
            "RETHLAS_REVIEW_ADAPTER_SHA256": "a" * 64,
            "RETHLAS_REVIEW_DB": "/trusted/hotjoin.sqlite3",
            server._REVIEW_RUN_ENV: run_id,
        }
        with mock.patch.dict(os.environ, static, clear=True):
            self.assertEqual(
                server._released_memory_registry_mode(
                    owner_manifest_snapshot_json=snapshot
                ),
                "owner_read_only_snapshot",
            )
            with mock.patch.object(
                server,
                "_adapter_memory_batch_publication_status",
                side_effect=AssertionError("snapshot mode must not call the adapter"),
            ):
                self.assertEqual(
                    server._memory_batch_registry_manifest(
                        problem_id,
                        owner_manifest_snapshot_json=snapshot,
                    ),
                    {},
                )

        with mock.patch.dict(
            os.environ,
            {**static, "RETHLAS_REVIEW_CONTROL_TOKEN": "b" * 64},
            clear=True,
        ):
            with self.assertRaisesRegex(ValueError, "forbids a raw review control"):
                server._released_memory_registry_mode(
                    owner_manifest_snapshot_json=snapshot
                )

    def test_owner_manifest_snapshot_env_cannot_authorize_app_or_mutation(
        self,
    ) -> None:
        snapshot_env = {
            server._OWNER_MEMORY_BATCH_MANIFEST_SNAPSHOT_ENV: "{}",
        }
        with mock.patch.dict(os.environ, snapshot_env, clear=True):
            with self.assertRaisesRegex(
                ValueError, "released reasoning registry is incomplete"
            ):
                server.memory_append_batch(
                    "snapshot-mutation-rejected",
                    [{"channel": "proof_steps", "record": {"claim": "no"}}],
                )
            with mock.patch.object(
                sys,
                "argv",
                ["server.py", "--generation-control-state", "frontier/example"],
            ):
                with self.assertRaisesRegex(
                    SystemExit, "valid only for the generation-control receipt CLI"
                ):
                    server.main()

    def test_owner_snapshot_excludes_forged_legacy_wait_jsonl_but_offline_reads_it(
        self,
    ) -> None:
        problem_id = "forged/wait"
        run_id = "run-forged-wait"
        event_id = "mem_forged_event"
        branch_id = "mem_forged_branch"
        server.memory_append(
            problem_id,
            "events",
            {
                "event_type": "recursive_proving_round",
                "status": "waiting_cost_gate",
            },
        )
        server.memory_append(
            problem_id,
            "branch_states",
            {
                "branch_id": "root",
                "state": {"status": "waiting_cost_gate"},
            },
        )
        # Use stable ids in the same legacy JSONL format a workspace writer can
        # forge, independently of the append helper's generated ids.
        event_path = server._channel_path(problem_id, "events")
        branch_path = server._channel_path(problem_id, "branch_states")
        event = json.loads(event_path.read_text(encoding="utf-8").splitlines()[0])
        branch = json.loads(branch_path.read_text(encoding="utf-8").splitlines()[0])
        event["record_id"] = event_id
        branch["record_id"] = branch_id
        event_path.write_text(json.dumps(event) + "\n", encoding="utf-8")
        branch_path.write_text(json.dumps(branch) + "\n", encoding="utf-8")

        offline = server._active_memory_records_by_id(problem_id)
        self.assertEqual(set(offline), {event_id, branch_id})
        server._validate_generation_wait_evidence(
            problem_id, "waiting_cost_gate", [event_id, branch_id]
        )

        snapshot = server.canonical_json_bytes(
            {
                "schema_version": "rethlas_memory_batch_publication_status_v1",
                "run_id": run_id,
                "problem_id": problem_id,
                "receipts": [],
            }
        ).decode("utf-8")
        static = {
            "RETHLAS_REVIEW_ADAPTER_PATH": "/trusted/hotjoin_adapter.py",
            "RETHLAS_REVIEW_ADAPTER_SHA256": "a" * 64,
            "RETHLAS_REVIEW_DB": "/trusted/hotjoin.sqlite3",
            server._REVIEW_RUN_ENV: run_id,
        }
        with mock.patch.dict(os.environ, static, clear=True):
            self.assertEqual(
                server._active_memory_records_by_id(
                    problem_id,
                    owner_manifest_snapshot_json=snapshot,
                ),
                {},
            )
            with self.assertRaisesRegex(ValueError, "not active memory"):
                server._validate_generation_wait_evidence(
                    problem_id,
                    "waiting_cost_gate",
                    [event_id, branch_id],
                    owner_manifest_snapshot_json=snapshot,
                )

    def test_memory_append_batch_cannot_publish_across_host_freeze(self) -> None:
        items = [
            {
                "channel": "proof_steps",
                "record": {"claim": "must remain before the review freeze"},
            }
        ]
        phase = {
            "review_due_at_utc": "2030-01-01T00:00:00+00:00",
            "review_due_monotonic": 1.0e18,
            "hard_stop_at_utc": "2030-01-01T01:00:00+00:00",
            "hard_stop_monotonic": 2.0e18,
        }
        cutoff = datetime.fromisoformat(phase["review_due_at_utc"]).timestamp()

        with (
            mock.patch.object(
                server.time, "time", side_effect=[cutoff - 1, cutoff + 1]
            ),
            mock.patch.object(server.time, "monotonic", side_effect=[1.0, 2.0]),
        ):
            with self.assertRaisesRegex(ValueError, "publication window is closed"):
                server.memory_append_batch(
                    "deadline/problem",
                    items,
                    _trusted_publication_preflight=lambda: phase,
                )

        checkpoint_dir = server._batch_checkpoint_dir("deadline/problem")
        data = next(checkpoint_dir.glob("batch_*.json"))
        self.assertFalse(data.name.endswith(".commit.json"))
        self.assertFalse(server._batch_commit_path(data).exists())
        self.assertEqual(
            server._load_memory_entries("deadline/problem")["proof_steps"], []
        )
        with self.assertRaisesRegex(ValueError, "not logically committed"):
            server._validate_memory_batch_checkpoint("deadline/problem", data)

        with (
            mock.patch.object(server.time, "time", return_value=cutoff - 1),
            mock.patch.object(server.time, "monotonic", return_value=1.0),
        ):
            receipt = server.memory_append_batch(
                "deadline/problem",
                items,
                _trusted_publication_preflight=lambda: phase,
            )
        self.assertEqual(receipt["status"], "ok")
        self.assertTrue(server._batch_commit_path(data).is_file())

    def test_memory_append_batch_prelink_witness_denial_is_never_visible(
        self,
    ) -> None:
        problem_id = "blocked-prelink-witness"
        items = [{"channel": "proof_steps", "record": {"claim": "fenced"}}]
        phase = {
            "review_due_at_utc": "2030-01-01T00:00:00+00:00",
            "review_due_monotonic": 1.0e18,
            "hard_stop_at_utc": "2030-01-01T01:00:00+00:00",
            "hard_stop_monotonic": 2.0e18,
        }
        cutoff = datetime.fromisoformat(phase["review_due_at_utc"]).timestamp()
        clock = {"wall": cutoff - 1.0}
        prelink_ready = threading.Event()
        release_prelink = threading.Event()
        fsynced_inodes: set[int] = set()
        witness = {"fsynced": False, "canonical": False}
        failures: list[BaseException] = []
        preflight_count = 0
        real_fsync = server.os.fsync

        def tracked_fsync(descriptor: int) -> None:
            real_fsync(descriptor)
            fsynced_inodes.add(server.os.fstat(descriptor).st_ino)

        def preflight() -> dict[str, object]:
            nonlocal preflight_count
            preflight_count += 1
            if preflight_count == 2:
                checkpoint_dir = server._batch_checkpoint_dir(problem_id)
                temporaries = list(checkpoint_dir.glob(".*.commit.*.tmp"))
                if len(temporaries) == 1:
                    temporary = temporaries[0]
                    witness["fsynced"] = (
                        temporary.stat().st_ino in fsynced_inodes
                    )
                    payload = json.loads(temporary.read_text(encoding="utf-8"))
                    witness["canonical"] = (
                        payload["schema"] == server.MEMORY_BATCH_COMMIT_SCHEMA
                        and payload["commit_sha256"]
                        == server._memory_batch_commit_sha256(payload)
                    )
                prelink_ready.set()
                if not release_prelink.wait(timeout=5):
                    raise TimeoutError("test did not release prelink witness")
            return phase

        def publish() -> None:
            try:
                server.memory_append_batch(
                    problem_id,
                    items,
                    _trusted_publication_preflight=preflight,
                )
            except BaseException as exc:
                failures.append(exc)

        with (
            mock.patch.object(server.os, "fsync", side_effect=tracked_fsync),
            mock.patch.object(server.time, "time", side_effect=lambda: clock["wall"]),
            mock.patch.object(server.time, "monotonic", return_value=1.0),
        ):
            publisher = threading.Thread(target=publish)
            publisher.start()
            self.assertTrue(prelink_ready.wait(timeout=5))
            checkpoint_dir = server._batch_checkpoint_dir(problem_id)
            data = next(checkpoint_dir.glob("batch_*.json"))
            self.assertTrue(witness["fsynced"])
            self.assertTrue(witness["canonical"])
            self.assertFalse(server._batch_commit_path(data).exists())
            self.assertEqual(
                server._load_memory_entries(problem_id)["proof_steps"], []
            )
            clock["wall"] = cutoff + 1.0
            release_prelink.set()
            publisher.join(timeout=5)

        self.assertFalse(publisher.is_alive())
        self.assertEqual(len(failures), 1)
        self.assertIsInstance(failures[0], ValueError)
        self.assertIn("publication window is closed", str(failures[0]))
        self.assertFalse(server._batch_commit_path(data).exists())
        self.assertEqual(list(checkpoint_dir.glob(".*.commit.*.tmp")), [])
        self.assertEqual(server._trusted_checkpoint_records(problem_id), {})

    def test_memory_append_batch_second_preflight_denial_leaves_invisible_orphan(
        self,
    ) -> None:
        allowed = {
            "review_due_at_utc": "2030-01-01T00:00:00+00:00",
            "review_due_monotonic": 1.0e18,
            "hard_stop_at_utc": "2030-01-01T01:00:00+00:00",
            "hard_stop_monotonic": 2.0e18,
        }
        denied = {**allowed, "review_due_at_utc": "2029-12-31T23:59:00+00:00"}
        phases = iter((allowed, denied))
        items = [{"channel": "proof_steps", "record": {"claim": "fenced"}}]

        with self.assertRaisesRegex(ValueError, "cutoff changed"):
            server.memory_append_batch(
                "second-preflight",
                items,
                _trusted_publication_preflight=lambda: next(phases),
            )
        checkpoint_dir = server._batch_checkpoint_dir("second-preflight")
        data = next(
            path
            for path in checkpoint_dir.glob("batch_*.json")
            if not path.name.endswith(".commit.json")
        )
        self.assertFalse(server._batch_commit_path(data).exists())
        self.assertEqual(server._trusted_checkpoint_records("second-preflight"), {})

        receipt = server.memory_append_batch(
            "second-preflight",
            items,
            _trusted_publication_preflight=lambda: allowed,
        )
        self.assertTrue(server._batch_commit_path(data).is_file())
        self.assertEqual(receipt["status"], "ok")

    def test_memory_append_batch_host_registry_rejection_keeps_marker_invisible(
        self,
    ) -> None:
        problem_id = "registry-rejected-after-link"
        items = [
            {
                "channel": "proof_steps",
                "record": {"claim": "prepared marker is not a logical commit"},
            }
        ]
        phase = {
            "review_due_at_utc": "2030-01-01T00:00:00+00:00",
            "review_due_monotonic": 1.0e18,
            "hard_stop_at_utc": "2030-01-01T01:00:00+00:00",
            "hard_stop_monotonic": 2.0e18,
        }
        rejected = {
            "schema_version": server.MEMORY_BATCH_PUBLICATION_RECEIPT_SCHEMA,
            "state": "rejected",
        }
        with (
            mock.patch.dict(
                os.environ, {server._REVIEW_RUN_ENV: "run-registry"}, clear=False
            ),
            mock.patch.object(
                server, "_released_memory_registry_configured", return_value=True
            ),
            mock.patch.object(
                server,
                "_adapter_memory_batch_publication_commit",
                return_value=rejected,
            ) as commit,
            mock.patch.object(
                server, "_memory_batch_registry_manifest", return_value={}
            ),
        ):
            with self.assertRaisesRegex(
                ValueError, "memory checkpoint publication was rejected"
            ):
                server.memory_append_batch(
                    problem_id,
                    items,
                    _trusted_publication_preflight=lambda: phase,
                )
            checkpoint = next(
                path
                for path in server._batch_checkpoint_dir(problem_id).glob(
                    "batch_*.json"
                )
                if not path.name.endswith(".commit.json")
            )
            self.assertTrue(server._batch_commit_path(checkpoint).is_file())
            self.assertEqual(server._trusted_checkpoint_records(problem_id), {})
        self.assertEqual(commit.call_count, 1)

    def test_memory_append_batch_host_acceptance_returns_bound_business_receipt(
        self,
    ) -> None:
        problem_id = "registry-accepted-business-receipt"

        def accept(**bindings: str) -> dict[str, object]:
            seed: dict[str, object] = {
                "schema_version": server.MEMORY_BATCH_PUBLICATION_RECEIPT_SCHEMA,
                "state": "accepted",
                "run_id": "run-registry",
                "problem_id": bindings["problem_id"],
                "batch_id": bindings["batch_id"],
                "checkpoint_sha256": bindings["checkpoint_sha256"],
                "commit_sha256": bindings["commit_sha256"],
                "publication_class": bindings["publication_class"],
                "cycle_id": "cycle_" + "1" * 32,
                "cutoff_action_id": "cadact_" + "2" * 32,
                "cutoff_kind": "review_1",
                "cutoff_at_utc": "2030-01-01T00:00:00+00:00",
                "cutoff_monotonic": 1.0e18,
                "accepted_at_utc": "2029-12-31T23:59:59+00:00",
                "accepted_at_monotonic": 1.0,
                "boot_identity": "test-boot",
            }
            seed["receipt_sha256"] = hashlib.sha256(
                json.dumps(seed, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            return seed

        with (
            mock.patch.dict(
                os.environ, {server._REVIEW_RUN_ENV: "run-registry"}, clear=False
            ),
            mock.patch.object(
                server, "_released_memory_registry_configured", return_value=True
            ),
            mock.patch.object(
                server,
                "_adapter_memory_batch_publication_commit",
                side_effect=accept,
            ),
        ):
            receipt = server.memory_append_batch(
                problem_id,
                [{"channel": "proof_steps", "record": {"claim": "admitted"}}],
            )
        self.assertEqual(receipt["schema_version"], server.MEMORY_BATCH_RECEIPT_SCHEMA)
        self.assertRegex(receipt["checkpoint_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(receipt["committed_at_utc"], "2029-12-31T23:59:59+00:00")
        self.assertEqual(receipt["committed_at_monotonic"], 1.0)
        self.assertEqual(receipt["publication_receipt"]["state"], "accepted")
        self.assertEqual(
            receipt["publication_receipt"]["checkpoint_sha256"],
            receipt["checkpoint_sha256"],
        )

    def test_registry_commit_waits_for_successful_artifact_and_directory_fsync(
        self,
    ) -> None:
        problem_id = "registry-retry-needs-durable-artifacts"
        items = [
            {"channel": "proof_steps", "record": {"claim": "durable before DB"}}
        ]
        prepared = server.memory_append_batch(problem_id, items)
        adapter_calls: list[dict[str, str]] = []

        def accepted(**bindings: str) -> dict[str, object]:
            adapter_calls.append(dict(bindings))
            seed: dict[str, object] = {
                "schema_version": server.MEMORY_BATCH_PUBLICATION_RECEIPT_SCHEMA,
                "state": "accepted",
                "run_id": "run-durable",
                "problem_id": bindings["problem_id"],
                "batch_id": bindings["batch_id"],
                "checkpoint_sha256": bindings["checkpoint_sha256"],
                "commit_sha256": bindings["commit_sha256"],
                "publication_class": bindings["publication_class"],
                "cycle_id": "cycle_" + "1" * 32,
                "cutoff_action_id": "cadact_" + "2" * 32,
                "cutoff_kind": "review_1",
                "cutoff_at_utc": "2030-01-01T00:00:00+00:00",
                "cutoff_monotonic": 1.0e18,
                "accepted_at_utc": "2029-12-31T23:59:59+00:00",
                "accepted_at_monotonic": 1.0,
                "boot_identity": "test-boot",
            }
            seed["receipt_sha256"] = hashlib.sha256(
                json.dumps(seed, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            return seed

        with (
            mock.patch.dict(
                os.environ, {server._REVIEW_RUN_ENV: "run-durable"}, clear=False
            ),
            mock.patch.object(
                server, "_released_memory_registry_configured", return_value=True
            ),
            mock.patch.object(
                server,
                "_adapter_memory_batch_publication_commit",
                side_effect=accepted,
            ),
            mock.patch.object(
                server,
                "_fsync_directory_fd",
                side_effect=OSError("persistent directory fsync failure"),
            ),
        ):
            with self.assertRaisesRegex(OSError, "directory fsync failure"):
                server.memory_append_batch(problem_id, items)
        self.assertEqual(adapter_calls, [])
        self.assertTrue(Path(prepared["checkpoint_path"]).is_file())
        self.assertTrue(
            server._batch_commit_path(Path(prepared["checkpoint_path"])).is_file()
        )

        with (
            mock.patch.dict(
                os.environ, {server._REVIEW_RUN_ENV: "run-durable"}, clear=False
            ),
            mock.patch.object(
                server, "_released_memory_registry_configured", return_value=True
            ),
            mock.patch.object(
                server,
                "_adapter_memory_batch_publication_commit",
                side_effect=accepted,
            ),
        ):
            recovered = server.memory_append_batch(problem_id, items)
        self.assertEqual(len(adapter_calls), 1)
        self.assertEqual(recovered["publication_receipt"]["state"], "accepted")

    def test_accepted_registry_manifest_fails_closed_on_missing_artifacts(self) -> None:
        for missing in ("checkpoint", "marker"):
            problem_id = f"registry-accepted-missing-{missing}"
            prepared = server.memory_append_batch(
                problem_id,
                [
                    {
                        "channel": "proof_steps",
                        "record": {"claim": f"official {missing} must exist"},
                    }
                ],
            )
            checkpoint = Path(prepared["checkpoint_path"])
            target = (
                checkpoint
                if missing == "checkpoint"
                else server._batch_commit_path(checkpoint)
            )
            target.unlink()
            manifest = {prepared["batch_id"]: {"state": "accepted"}}
            with (
                mock.patch.object(
                    server, "_released_memory_registry_configured", return_value=True
                ),
                mock.patch.object(
                    server,
                    "_memory_batch_registry_manifest",
                    return_value=manifest,
                ),
            ):
                with self.assertRaisesRegex(
                    ValueError,
                    "missing checkpoint artifacts|lacks its commit marker",
                ):
                    server._trusted_checkpoint_records(problem_id)

    def test_released_projection_ignores_corrupt_unregistered_artifacts(self) -> None:
        problem_id = "registry-unregistered-corrupt-inert"
        prepared = server.memory_append_batch(
            problem_id,
            [{"channel": "proof_steps", "record": {"claim": "official"}}],
        )
        corrupt = (
            server._batch_checkpoint_dir(problem_id)
            / ("batch_" + "f" * 64 + ".json")
        )
        corrupt.write_bytes(b"{not-json\n")
        accepted = {
            "schema_version": server.MEMORY_BATCH_PUBLICATION_RECEIPT_SCHEMA,
            "state": "accepted",
            "run_id": "run-inert",
            "problem_id": problem_id,
            "batch_id": prepared["batch_id"],
            "checkpoint_sha256": prepared["checkpoint_sha256"],
            "commit_sha256": prepared["commit_sha256"],
            "publication_class": "reasoning_checkpoint",
            "accepted_at_utc": "2026-08-10T00:00:01+00:00",
            "accepted_at_monotonic": 1.0,
        }
        with (
            mock.patch.dict(
                os.environ, {server._REVIEW_RUN_ENV: "run-inert"}, clear=False
            ),
            mock.patch.object(
                server, "_released_memory_registry_configured", return_value=True
            ),
            mock.patch.object(
                server,
                "_memory_batch_registry_manifest",
                return_value={prepared["batch_id"]: accepted},
            ),
        ):
            trusted = server._trusted_checkpoint_records(problem_id)
            self.assertEqual(len(trusted), 1)
            self.assertEqual(next(iter(trusted.values()))["record"], {"claim": "official"})
        with (
            mock.patch.object(
                server, "_released_memory_registry_configured", return_value=True
            ),
            mock.patch.object(
                server, "_memory_batch_registry_manifest", return_value={}
            ),
        ):
            self.assertEqual(server._trusted_checkpoint_records(problem_id), {})

    def test_memory_append_batch_is_independent_of_partial_memory_init(self) -> None:
        problem_id = "partial-init"
        problem_dir = server._problem_dir(problem_id)
        problem_dir.mkdir(parents=True)
        invalid_meta = problem_dir / "meta.json"
        invalid_meta.write_text('{"problem_id":', encoding="utf-8")
        items = [
            {
                "channel": "proof_steps",
                "record": {"claim": "batch survives an interrupted legacy init"},
            }
        ]

        with mock.patch.object(
            server,
            "memory_init",
            side_effect=AssertionError("batch must not call memory_init"),
        ):
            first = server.memory_append_batch(problem_id, items)
            retry = server.memory_append_batch(problem_id, items)

        self.assertEqual(retry, first)
        self.assertEqual(invalid_meta.read_text(encoding="utf-8"), '{"problem_id":')
        self.assertEqual(
            len(list(server._batch_checkpoint_dir(problem_id).glob("batch_*.json"))),
            1,
        )

    def test_memory_init_atomic_failure_preserves_meta_and_retry_updates(self) -> None:
        problem_id = "atomic-meta"
        first = server.memory_init(problem_id, {"generation": 1})
        meta_path = Path(first["meta_path"])
        original = meta_path.read_bytes()

        with mock.patch.object(
            server.os,
            "replace",
            side_effect=OSError("injected metadata replace failure"),
        ):
            with self.assertRaisesRegex(OSError, "injected metadata replace failure"):
                server.memory_init(problem_id, {"generation": 2})

        self.assertEqual(meta_path.read_bytes(), original)
        self.assertEqual(
            json.loads(meta_path.read_text(encoding="utf-8"))["generation"], 1
        )
        self.assertEqual(list(meta_path.parent.glob(".meta.json.*.tmp")), [])

        server.memory_init(problem_id, {"generation": 2})
        self.assertEqual(
            json.loads(meta_path.read_text(encoding="utf-8"))["generation"], 2
        )

    def test_memory_init_concurrent_updates_are_serialized_and_complete(self) -> None:
        problem_id = "concurrent-meta"
        worker_count = 6
        start = threading.Barrier(worker_count)

        def initialize(index: int) -> None:
            start.wait(timeout=10)
            server.memory_init(problem_id, {f"worker_{index}": index})

        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            list(executor.map(initialize, range(worker_count)))

        meta_path = server._problem_dir(problem_id) / "meta.json"
        loaded = json.loads(meta_path.read_text(encoding="utf-8"))
        self.assertEqual(
            {key: loaded[key] for key in loaded if key.startswith("worker_")},
            {f"worker_{index}": index for index in range(worker_count)},
        )
        self.assertEqual(list(meta_path.parent.glob(".meta.json.*.tmp")), [])

    def test_batch_creation_and_temp_cleanup_have_parent_fsyncs(self) -> None:
        problem_id = "durability-trace"
        problem_dir = server._problem_dir(problem_id)
        checkpoint_dir = server._batch_checkpoint_dir(problem_id)
        operations: list[tuple[str, Path]] = []
        real_fsync_directory_fd = server._fsync_directory_fd
        real_cleanup = server._unlink_temporary_durable_at

        def trace_fsync(descriptor: int, path: Path) -> None:
            operations.append(("fsync", path))
            real_fsync_directory_fd(descriptor, path)

        def trace_cleanup(
            parent_descriptor: int,
            parent_path: Path,
            name: str,
            descriptor: int | None,
        ) -> None:
            operations.append(("cleanup", parent_path))
            real_cleanup(parent_descriptor, parent_path, name, descriptor)

        with (
            mock.patch.object(server, "_fsync_directory_fd", side_effect=trace_fsync),
            mock.patch.object(
                server, "_unlink_temporary_durable_at", side_effect=trace_cleanup
            ),
            mock.patch.object(
                server.os,
                "link",
                side_effect=OSError("injected pre-publication link failure"),
            ),
        ):
            with self.assertRaisesRegex(
                OSError, "injected pre-publication link failure"
            ):
                server.memory_append_batch(
                    problem_id,
                    [{"channel": "events", "record": {"phase": "root"}}],
                )

        self.assertIn(("fsync", server._memory_root_path()), operations)
        self.assertIn(("fsync", problem_dir), operations)
        cleanup_index = operations.index(("cleanup", checkpoint_dir))
        self.assertEqual(
            operations[cleanup_index + 1],
            ("fsync", checkpoint_dir),
        )
        self.assertEqual(list(checkpoint_dir.glob(".*.tmp")), [])

    def test_batch_directory_parent_fsync_failures_are_retry_safe(self) -> None:
        items = [{"channel": "events", "record": {"phase": "root"}}]
        for problem_id, parent_selector in (
            ("problem-parent-failure", "problem"),
            ("checkpoint-parent-failure", "checkpoint"),
        ):
            with self.subTest(parent_selector=parent_selector):
                problem_dir = server._problem_dir(problem_id)
                failing_parent = (
                    server._memory_root_path()
                    if parent_selector == "problem"
                    else problem_dir
                )
                real_fsync_directory_fd = server._fsync_directory_fd
                failed = False
                attempted = 0

                def fail_once(descriptor: int, path: Path) -> None:
                    nonlocal failed, attempted
                    if path == failing_parent:
                        attempted += 1
                        if not failed:
                            failed = True
                            raise OSError("injected parent fsync failure")
                    real_fsync_directory_fd(descriptor, path)

                with mock.patch.object(
                    server,
                    "_fsync_directory_fd",
                    side_effect=fail_once,
                ):
                    with self.assertRaisesRegex(
                        OSError, "injected parent fsync failure"
                    ):
                        server.memory_append_batch(problem_id, items)
                    receipt = server.memory_append_batch(problem_id, items)

                self.assertGreaterEqual(attempted, 2)
                self.assertTrue(Path(receipt["checkpoint_path"]).is_file())

    def test_memory_root_symlink_cannot_redirect_batch_outside(self) -> None:
        configured_parent = server.MEMORY_ROOT
        attack_root = configured_parent / "memory-root"
        with tempfile.TemporaryDirectory() as outside_directory:
            outside = Path(outside_directory)
            attack_root.symlink_to(outside, target_is_directory=True)
            server.MEMORY_ROOT = attack_root
            try:
                with self.assertRaises((OSError, ValueError)):
                    server.memory_append_batch(
                        "root-escape",
                        [{"channel": "events", "record": {"attack": True}}],
                    )
            finally:
                server.MEMORY_ROOT = configured_parent
                attack_root.unlink()

            self.assertEqual(list(outside.iterdir()), [])

    def test_problem_directory_symlink_cannot_redirect_append_outside(self) -> None:
        with tempfile.TemporaryDirectory() as outside_directory:
            outside = Path(outside_directory)
            sentinel = outside / "events.jsonl"
            sentinel.write_text("outside-sentinel\n", encoding="utf-8")
            (server.MEMORY_ROOT / "problem-escape").symlink_to(
                outside,
                target_is_directory=True,
            )

            with self.assertRaises((OSError, ValueError)):
                server.memory_append(
                    "problem-escape",
                    "events",
                    {"attack": True},
                )

            self.assertEqual(sentinel.read_text(encoding="utf-8"), "outside-sentinel\n")

    def test_channel_symlink_and_hardlink_cannot_redirect_append(self) -> None:
        problem_id = "channel-escape"
        server.memory_init(problem_id)
        channel = server._channel_path(problem_id, "events")
        with tempfile.TemporaryDirectory() as outside_directory:
            outside = Path(outside_directory) / "outside.jsonl"
            outside.write_text("outside-sentinel\n", encoding="utf-8")

            channel.unlink()
            channel.symlink_to(outside)
            with self.assertRaises((OSError, ValueError)):
                server.memory_append(problem_id, "events", {"attack": "symlink"})
            channel.unlink()

            channel.hardlink_to(outside)
            with self.assertRaises((OSError, ValueError)):
                server.memory_append(problem_id, "events", {"attack": "hardlink"})
            channel.unlink()

            self.assertEqual(outside.read_text(encoding="utf-8"), "outside-sentinel\n")

    def test_meta_and_lock_links_are_rejected_without_outside_writes(self) -> None:
        problem_id = "metadata-escape"
        initialized = server.memory_init(problem_id, {"safe": True})
        problem_dir = Path(initialized["memory_dir"])
        meta_path = Path(initialized["meta_path"])
        lock_path = problem_dir / ".meta.lock"
        with tempfile.TemporaryDirectory() as outside_directory:
            outside = Path(outside_directory) / "outside.json"
            outside.write_text('{"outside":true}\n', encoding="utf-8")

            meta_path.unlink()
            meta_path.symlink_to(outside)
            with self.assertRaises((OSError, ValueError)):
                server.memory_init(problem_id, {"attack": "meta-symlink"})
            meta_path.unlink()

            meta_path.hardlink_to(outside)
            with self.assertRaises((OSError, ValueError)):
                server.memory_init(problem_id, {"attack": "meta-hardlink"})
            meta_path.unlink()

            lock_path.unlink()
            lock_path.symlink_to(outside)
            with self.assertRaises((OSError, ValueError)):
                server.memory_init(problem_id, {"attack": "lock-symlink"})
            lock_path.unlink()

            lock_path.hardlink_to(outside)
            with self.assertRaises((OSError, ValueError)):
                server.memory_init(problem_id, {"attack": "lock-hardlink"})
            lock_path.unlink()

            self.assertEqual(outside.read_text(encoding="utf-8"), '{"outside":true}\n')

    def test_checkpoint_directory_and_file_links_are_rejected(self) -> None:
        items = [{"channel": "events", "record": {"phase": "root"}}]
        problem_id = "checkpoint-dir-escape"
        server.memory_init(problem_id)
        checkpoint_dir = server._batch_checkpoint_dir(problem_id)
        with tempfile.TemporaryDirectory() as outside_directory:
            outside_dir = Path(outside_directory)
            checkpoint_dir.symlink_to(outside_dir, target_is_directory=True)
            with self.assertRaises((OSError, ValueError)):
                server.memory_append_batch(problem_id, items)
            checkpoint_dir.unlink()
            self.assertEqual(list(outside_dir.iterdir()), [])

        problem_id = "checkpoint-file-escape"
        receipt = server.memory_append_batch(problem_id, items)
        checkpoint = Path(receipt["checkpoint_path"])
        original_bytes = checkpoint.read_bytes()
        with tempfile.TemporaryDirectory() as outside_directory:
            outside = Path(outside_directory) / "outside.json"
            outside.write_bytes(original_bytes)

            checkpoint.unlink()
            checkpoint.symlink_to(outside)
            with self.assertRaises((OSError, ValueError)):
                server.memory_append_batch(problem_id, items)
            checkpoint.unlink()

            checkpoint.hardlink_to(outside)
            with self.assertRaises((OSError, ValueError)):
                server.memory_append_batch(problem_id, items)
            checkpoint.unlink()

            self.assertEqual(outside.read_bytes(), original_bytes)

    def test_channel_post_open_inode_swap_is_detected_before_append(self) -> None:
        problem_id = "channel-toctou"
        server.memory_init(problem_id)
        channel = server._channel_path(problem_id, "events")
        displaced = channel.with_name("events.displaced")
        with tempfile.TemporaryDirectory() as outside_directory:
            outside = Path(outside_directory) / "outside.jsonl"
            outside.write_text("outside-sentinel\n", encoding="utf-8")
            real_open = server.os.open
            swapped = False

            def swap_after_open(
                path: str | Path,
                flags: int,
                *args: object,
                **kwargs: object,
            ) -> int:
                nonlocal swapped
                descriptor = real_open(path, flags, *args, **kwargs)
                if (
                    path == "events.jsonl"
                    and flags & server.os.O_APPEND
                    and not swapped
                ):
                    swapped = True
                    channel.rename(displaced)
                    channel.symlink_to(outside)
                return descriptor

            try:
                with mock.patch.object(server.os, "open", side_effect=swap_after_open):
                    with self.assertRaises((OSError, ValueError)):
                        server.memory_append(problem_id, "events", {"attack": True})
            finally:
                if channel.is_symlink():
                    channel.unlink()
                if displaced.exists():
                    displaced.rename(channel)

            self.assertTrue(swapped)
            self.assertEqual(outside.read_text(encoding="utf-8"), "outside-sentinel\n")

    def test_memory_append_batch_prepublish_failure_retry_exposes_one_batch(
        self,
    ) -> None:
        prior = server.memory_append(
            "atomic-failure",
            "proof_steps",
            {"claim": "prior route remains active"},
        )

        with mock.patch.object(
            server.os,
            "link",
            side_effect=OSError("injected pre-commit failure"),
        ):
            with self.assertRaisesRegex(OSError, "injected pre-commit failure"):
                server.memory_append_batch(
                    "atomic-failure",
                    [
                        {
                            "channel": "proof_steps",
                            "record": {"claim": "uncommitted replacement"},
                            "supersedes": [prior["record_id"]],
                        },
                        {
                            "channel": "failed_paths",
                            "record": {"obstruction": "must remain invisible"},
                        },
                    ],
                )

        logical = server._load_memory_entries("atomic-failure")
        self.assertEqual(len(logical["proof_steps"]), 1)
        self.assertEqual(logical["proof_steps"][0]["record_id"], prior["record_id"])
        self.assertTrue(logical["proof_steps"][0]["effective_active"])
        self.assertEqual(logical["failed_paths"], [])
        checkpoint_dir = server._batch_checkpoint_dir("atomic-failure")
        self.assertEqual(list(checkpoint_dir.glob("batch_*.json")), [])
        self.assertEqual(list(checkpoint_dir.glob(".*.tmp")), [])

        retry = server.memory_append_batch(
            "atomic-failure",
            [
                {
                    "channel": "proof_steps",
                    "record": {"claim": "uncommitted replacement"},
                    "supersedes": [prior["record_id"]],
                },
                {
                    "channel": "failed_paths",
                    "record": {"obstruction": "must remain invisible"},
                },
            ],
        )
        self.assertEqual(
            [path.name for path in checkpoint_dir.glob("batch_*.json")],
            [f"{retry['batch_id']}.json"],
        )
        committed = server._load_memory_entries("atomic-failure")
        self.assertEqual(len(committed["failed_paths"]), 1)
        self.assertEqual(
            committed["proof_steps"][-1]["record_id"],
            retry["records"][0]["record_id"],
        )

    def test_memory_append_batch_postpublish_fsync_failure_retry_is_idempotent(
        self,
    ) -> None:
        items = [
            {
                "channel": "proof_steps",
                "record": {"claim": "durable retry checkpoint"},
            },
            {
                "channel": "branch_states",
                "record": {"state": "critic_pending"},
            },
        ]
        checkpoint_dir = server._batch_checkpoint_dir("post-publish-retry")
        real_fsync_directory_fd = server._fsync_directory_fd
        real_link = server.os.link
        failure_injected = False
        published = False

        def track_link(
            source: str,
            destination: str,
            *,
            src_dir_fd: int,
            dst_dir_fd: int,
            follow_symlinks: bool,
        ) -> None:
            nonlocal published
            real_link(
                source,
                destination,
                src_dir_fd=src_dir_fd,
                dst_dir_fd=dst_dir_fd,
                follow_symlinks=follow_symlinks,
            )
            published = True

        def fail_postpublish_fsync(descriptor: int, path: Path) -> None:
            nonlocal failure_injected
            if path == checkpoint_dir and published and not failure_injected:
                failure_injected = True
                raise OSError("injected post-publication fsync failure")
            real_fsync_directory_fd(descriptor, path)

        with (
            mock.patch.object(server.os, "link", side_effect=track_link),
            mock.patch.object(
                server,
                "_fsync_directory_fd",
                side_effect=fail_postpublish_fsync,
            ),
        ):
            with self.assertRaisesRegex(
                OSError, "injected post-publication fsync failure"
            ):
                server.memory_append_batch("post-publish-retry", items)

        published_paths = list(checkpoint_dir.glob("batch_*.json"))
        self.assertEqual(len(published_paths), 1)
        self.assertEqual(list(checkpoint_dir.glob(".*.tmp")), [])

        retry = server.memory_append_batch("post-publish-retry", items)
        self.assertEqual(Path(retry["checkpoint_path"]), published_paths[0])
        self.assertEqual(
            [path.name for path in checkpoint_dir.glob("batch_*.json")],
            [f"{retry['batch_id']}.json"],
        )
        logical = server._load_memory_entries("post-publish-retry")
        self.assertEqual(len(logical["proof_steps"]), 1)
        self.assertEqual(len(logical["branch_states"]), 1)
        self.assertEqual(len(logical["events"]), 1)

    def _sigkill_batch_at_cut(
        self,
        problem_id: str,
        items: list[dict[str, object]],
        cut: str,
    ) -> None:
        self.assertIn(cut, {"prelink", "postlink"})
        marker = server.MEMORY_ROOT / f".sigkill-{cut}-{time.time_ns()}.marker"
        child_source = textwrap.dedent(
            """
            import json
            import os
            import signal
            import sys
            from pathlib import Path

            from agents.generation.mcp import server

            server.MEMORY_ROOT = Path(sys.argv[1])
            marker = Path(sys.argv[2])
            problem_id = sys.argv[3]
            items = json.loads(sys.argv[4])
            cut = sys.argv[5]
            real_fsync = server._fsync_directory_fd

            def stop_before_link(
                source,
                destination,
                *,
                src_dir_fd,
                dst_dir_fd,
                follow_symlinks,
            ):
                del (
                    source,
                    destination,
                    src_dir_fd,
                    dst_dir_fd,
                    follow_symlinks,
                )
                marker.write_text("pre-link", encoding="utf-8")
                while True:
                    signal.pause()

            def stop_after_published_fsync(descriptor, path):
                real_fsync(descriptor, path)
                names = os.listdir(descriptor)
                has_final = any(
                    name.startswith("batch_") and name.endswith(".json")
                    for name in names
                )
                has_temp = any(
                    name.startswith(".batch_") and name.endswith(".tmp")
                    for name in names
                )
                if path.name == ".phase_checkpoints" and has_final and has_temp:
                    marker.write_text("post-publication-fsync", encoding="utf-8")
                    while True:
                        signal.pause()

            if cut == "prelink":
                server.os.link = stop_before_link
            elif cut == "postlink":
                server._fsync_directory_fd = stop_after_published_fsync
            else:
                raise ValueError(f"unknown cut: {cut}")
            server.memory_append_batch(problem_id, items)
            """
        )
        environment = dict(os.environ)
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        process = subprocess.Popen(
            [
                sys.executable,
                "-B",
                "-c",
                child_source,
                str(server.MEMORY_ROOT),
                str(marker),
                problem_id,
                json.dumps(items, ensure_ascii=False),
                cut,
            ],
            cwd=Path(__file__).resolve().parents[3],
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        communicated = False
        try:
            deadline = time.monotonic() + 10
            while not marker.exists() and process.poll() is None:
                if time.monotonic() >= deadline:
                    self.fail(f"child did not reach the {cut} cut")
                time.sleep(0.01)
            if not marker.exists():
                _stdout, stderr = process.communicate(timeout=2)
                communicated = True
                self.fail(f"child exited before the {cut} cut: {stderr}")
            process.kill()
            _stdout, stderr = process.communicate(timeout=5)
            communicated = True
            self.assertEqual(
                process.returncode,
                -signal.SIGKILL,
                msg=f"unexpected child stderr: {stderr}",
            )
        finally:
            if not communicated:
                if process.poll() is None:
                    process.kill()
                process.communicate(timeout=5)
            for pipe in (process.stdout, process.stderr):
                if pipe is not None and not pipe.closed:
                    pipe.close()
            marker.unlink(missing_ok=True)

    def test_sigkill_after_checkpoint_fsync_recovers_exact_orphan(self) -> None:
        problem_id = "sigkill-checkpoint-cut"
        items = [
            {
                "channel": "proof_steps",
                "record": {"claim": "survives a post-fsync SIGKILL"},
            }
        ]
        self._sigkill_batch_at_cut(problem_id, items, "postlink")

        checkpoint_dir = server._batch_checkpoint_dir(problem_id)
        finals = list(checkpoint_dir.glob("batch_*.json"))
        temporaries = list(checkpoint_dir.glob(".*.tmp"))
        self.assertEqual(len(finals), 1)
        self.assertEqual(len(temporaries), 1)
        self.assertEqual(finals[0].stat().st_ino, temporaries[0].stat().st_ino)
        self.assertEqual(finals[0].stat().st_nlink, 2)

        retry = server.memory_append_batch(problem_id, items)
        self.assertEqual(Path(retry["checkpoint_path"]), finals[0])
        self.assertEqual(list(checkpoint_dir.glob("batch_*.json")), finals)
        self.assertEqual(list(checkpoint_dir.glob(".*.tmp")), [])
        self.assertEqual(finals[0].stat().st_nlink, 1)
        logical = server._load_memory_entries(problem_id)
        self.assertEqual(len(logical["proof_steps"]), 1)
        self.assertEqual(len(logical["events"]), 1)

    def test_read_first_recovers_authenticated_postlink_orphan(self) -> None:
        problem_id = "read-first-checkpoint-cut"
        items = [
            {
                "channel": "proof_steps",
                "record": {"claim": "read performs constrained recovery"},
            }
        ]
        self._sigkill_batch_at_cut(problem_id, items, "postlink")

        checkpoint_dir = server._batch_checkpoint_dir(problem_id)
        checkpoint = next(checkpoint_dir.glob("batch_*.json"))
        self.assertEqual(checkpoint.stat().st_nlink, 2)

        logical = server._load_memory_entries(problem_id)

        self.assertEqual(logical["proof_steps"], [])
        self.assertEqual(logical["events"], [])
        self.assertEqual(checkpoint.stat().st_nlink, 1)
        self.assertEqual(list(checkpoint_dir.glob(".*.tmp")), [])

    def test_repeated_sigkill_preserves_stale_temp_and_recovers_true_orphan(
        self,
    ) -> None:
        problem_id = "repeated-sigkill-checkpoint-cut"
        items = [
            {
                "channel": "failed_paths",
                "record": {"obstruction": "multi-cut recovery"},
            }
        ]
        checkpoint_dir = server._batch_checkpoint_dir(problem_id)

        self._sigkill_batch_at_cut(problem_id, items, "prelink")
        self.assertEqual(list(checkpoint_dir.glob("batch_*.json")), [])
        prelink_temporaries = list(checkpoint_dir.glob(".*.tmp"))
        self.assertEqual(len(prelink_temporaries), 1)
        stale = prelink_temporaries[0]
        stale_inode = stale.stat().st_ino
        self.assertEqual(stale.stat().st_nlink, 1)

        self._sigkill_batch_at_cut(problem_id, items, "postlink")
        finals = list(checkpoint_dir.glob("batch_*.json"))
        temporaries = list(checkpoint_dir.glob(".*.tmp"))
        self.assertEqual(len(finals), 1)
        self.assertEqual(len(temporaries), 2)
        final = finals[0]
        self.assertEqual(final.stat().st_nlink, 2)
        same_inode = [
            temporary
            for temporary in temporaries
            if temporary.stat().st_ino == final.stat().st_ino
        ]
        self.assertEqual(len(same_inode), 1)
        self.assertEqual(stale.stat().st_ino, stale_inode)
        self.assertNotEqual(stale_inode, final.stat().st_ino)

        retry = server.memory_append_batch(problem_id, items)

        self.assertEqual(Path(retry["checkpoint_path"]), final)
        self.assertEqual(final.stat().st_nlink, 1)
        remaining_temporaries = list(checkpoint_dir.glob(".*.tmp"))
        self.assertEqual(remaining_temporaries, [stale])
        self.assertEqual(stale.stat().st_ino, stale_inode)
        self.assertEqual(stale.stat().st_nlink, 1)
        logical = server._load_memory_entries(problem_id)
        self.assertEqual(len(logical["failed_paths"]), 1)
        self.assertEqual(len(logical["events"]), 1)

    def test_checkpoint_orphan_recovery_fails_closed_when_link_count_ambiguous(
        self,
    ) -> None:
        problem_id = "ambiguous-checkpoint-cut"
        items = [{"channel": "events", "record": {"phase": "root"}}]
        receipt = server.memory_append_batch(problem_id, items)
        checkpoint = Path(receipt["checkpoint_path"])
        first_orphan = checkpoint.with_name(f".{checkpoint.name}.{'a' * 32}.tmp")
        second_orphan = checkpoint.with_name(f".{checkpoint.name}.{'b' * 32}.tmp")
        first_orphan.hardlink_to(checkpoint)
        second_orphan.hardlink_to(checkpoint)

        with self.assertRaisesRegex(ValueError, "unsafe hard-link count"):
            server._load_memory_entries(problem_id)

        self.assertTrue(first_orphan.is_file())
        self.assertTrue(second_orphan.is_file())
        self.assertEqual(checkpoint.stat().st_nlink, 3)

    def test_checkpoint_orphan_recovery_fails_closed_without_same_inode(
        self,
    ) -> None:
        problem_id = "no-same-inode-checkpoint-cut"
        receipt = server.memory_append_batch(
            problem_id,
            [{"channel": "events", "record": {"phase": "root"}}],
        )
        checkpoint = Path(receipt["checkpoint_path"])
        outside_name = checkpoint.with_name("outside-hardlink")
        outside_name.hardlink_to(checkpoint)
        unrelated = checkpoint.with_name(f".{checkpoint.name}.{'c' * 32}.tmp")
        unrelated.write_bytes(b"unrelated-stale-temp\n")

        with self.assertRaisesRegex(ValueError, "no unique same-inode orphan"):
            server._load_memory_entries(problem_id)

        self.assertTrue(outside_name.is_file())
        self.assertEqual(unrelated.read_bytes(), b"unrelated-stale-temp\n")
        self.assertEqual(checkpoint.stat().st_nlink, 2)

    def test_memory_append_batch_exact_retry_returns_original_receipt(self) -> None:
        items = [
            {
                "channel": "proof_steps",
                "record": {"claim": "same checkpoint"},
                "active": True,
                "supersedes": ["mem_old"],
            }
        ]

        with mock.patch.object(
            server,
            "_utc_now",
            side_effect=(
                "2026-08-10T00:00:00+00:00",
                "2026-08-10T01:00:00+00:00",
            ),
        ):
            first = server.memory_append_batch("exact-retry", items)
            second = server.memory_append_batch("exact-retry", items)

        self.assertEqual(second, first)
        self.assertEqual(first["timestamp_utc"], "2026-08-10T00:00:00+00:00")
        checkpoint_dir = server._batch_checkpoint_dir("exact-retry")
        self.assertEqual(len(list(checkpoint_dir.glob("batch_*.json"))), 1)
        logical = server._load_memory_entries("exact-retry")
        self.assertEqual(len(logical["proof_steps"]), 1)
        self.assertEqual(len(logical["events"]), 1)

    def test_memory_append_batch_rejects_noncanonical_receipt_inputs(self) -> None:
        with self.assertRaisesRegex(ValueError, "problem_id must already"):
            server.memory_append_batch(
                "category//problem/",
                [{"channel": "events", "record": {"phase": "root"}}],
            )
        for supersedes in (["mem_old", "mem_old"], [" mem_old"], None):
            with self.subTest(supersedes=supersedes):
                with self.assertRaisesRegex(ValueError, "supersedes must already"):
                    server.memory_append_batch(
                        "canonical-problem",
                        [
                            {
                                "channel": "proof_steps",
                                "record": {"claim": "x"},
                                "supersedes": supersedes,
                            }
                        ],
                    )

    def test_legacy_jsonl_corruption_fails_closed(self) -> None:
        path = server._channel_path("corrupt-jsonl", "proof_steps")
        server.memory_init("corrupt-jsonl")
        path.write_text('{"record_id":"ok"}\n{"torn":\n', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "invalid JSONL"):
            list(server._iter_jsonl(path))

    def test_memory_init_refreshes_updated_at_without_changing_created_at(self) -> None:
        with mock.patch.object(
            server,
            "_utc_now",
            side_effect=(
                "2026-08-10T00:00:00+00:00",
                "2026-08-10T01:00:00+00:00",
            ),
        ):
            first = server.memory_init("meta-refresh")
            second = server.memory_init("meta-refresh")
        first_meta = json.loads(Path(first["meta_path"]).read_text(encoding="utf-8"))
        second_meta = json.loads(Path(second["meta_path"]).read_text(encoding="utf-8"))
        assert first_meta["created_at_utc"] == "2026-08-10T00:00:00+00:00"
        assert second_meta["created_at_utc"] == first_meta["created_at_utc"]
        assert second_meta["updated_at_utc"] == "2026-08-10T01:00:00+00:00"

    def test_legacy_verify_helper_supplies_a_canonical_deadline(self) -> None:
        profile = mock.Mock()
        profile.json.return_value = {
            "schema_version": "rethlas_verifier_profile_v1",
            "service_version": "test-0.5.0",
            "profile": "compatible",
            "passes": [
                {
                    "pass_index": index,
                    "adapter": "codex_cli",
                    "provider": "openai",
                    "model": "gpt-6-astra",
                    "launch_model": "gpt-6-astra",
                    "reasoning_effort": "xhigh",
                    "session_mode": "cold",
                }
                for index in (1, 2)
            ],
            "automatic_tiebreaker": False,
            "fallback_policy": "forbid",
        }
        with (
            mock.patch.object(server.requests, "get", return_value=profile),
            mock.patch.object(
                server.requests,
                "post",
                side_effect=RuntimeError("stop after request capture"),
            ) as post,
        ):
            with self.assertRaisesRegex(RuntimeError, "request capture"):
                server.verify_proof_service("S", "proof", timeout_seconds=17)
        payload = post.call_args.kwargs["json"]
        deadline = datetime.fromisoformat(payload["verification_deadline_utc"])
        self.assertIsNotNone(deadline.tzinfo)
        self.assertGreater(deadline.timestamp(), time.time())
        self.assertEqual(payload["verification_pass_index"], 1)
        self.assertRegex(payload["verification_pass_identity"], r"^[0-9a-f]{64}$")

    def test_commit_wall_clock_rollback_is_clamped_to_checkpoint_time(self) -> None:
        with (
            mock.patch.object(
                server, "_utc_now", return_value="2030-01-01T00:00:00+00:00"
            ),
            mock.patch.object(
                server,
                "_require_memory_batch_publication_open",
                return_value=(1.0, 123.0),
            ),
        ):
            receipt = server.memory_append_batch(
                "clock-rollback",
                [{"channel": "events", "record": {"phase": "root"}}],
            )
        self.assertEqual(receipt["timestamp_utc"], "2030-01-01T00:00:00+00:00")
        self.assertEqual(receipt["committed_at_utc"], receipt["timestamp_utc"])

    def test_memory_append_batch_exact_retry_replays_winner_without_clock_sampling(
        self,
    ) -> None:
        items = [
            {
                "channel": "proof_steps",
                "record": {"claim": "stable committed winner"},
            }
        ]
        first = server.memory_append_batch("exact-winner-replay", items)

        def unexpected_preflight() -> None:
            raise AssertionError("a committed exact replay must not call the host")

        with (
            mock.patch.object(
                server.time,
                "time",
                side_effect=AssertionError("exact replay sampled the wall clock"),
            ),
            mock.patch.object(
                server.time,
                "monotonic",
                side_effect=AssertionError("exact replay sampled the monotonic clock"),
            ),
            mock.patch.object(
                server,
                "_utc_now",
                side_effect=AssertionError("exact replay created a new timestamp"),
            ),
        ):
            retry = server.memory_append_batch(
                "exact-winner-replay",
                items,
                _trusted_publication_preflight=unexpected_preflight,
            )

        self.assertEqual(retry, first)
        self.assertEqual(retry["committed_at_utc"], first["committed_at_utc"])
        self.assertEqual(retry["commit_sha256"], first["commit_sha256"])

    def test_mcp_exact_batch_replay_survives_phase_close_but_new_write_does_not(
        self,
    ) -> None:
        items = [
            {
                "channel": "proof_steps",
                "record": {"claim": "published before phase close"},
            }
        ]
        first = server.memory_append_batch("mcp-winner-replay", items)
        functions: dict[str, object] = {}

        class FakeMCP:
            def __init__(self, _name: str) -> None:
                pass

            def tool(self, *, name: str):
                def register(function):
                    functions[name] = function
                    return function

                return register

        def closed(_tool_name: str) -> None:
            raise ValueError("phase is closed")

        with (
            mock.patch.object(server, "FastMCP", FakeMCP),
            mock.patch.object(server, "_context_rehydrate_preflight", return_value=None),
            mock.patch.object(server, "_reasoning_phase_preflight", side_effect=closed),
        ):
            server.build_mcp_app()
            batch_tool = functions["memory_append_batch"]
            assert callable(batch_tool)
            replay = batch_tool("mcp-winner-replay", items)
            rejected = batch_tool(
                "mcp-winner-replay",
                [
                    {
                        "channel": "proof_steps",
                        "record": {"claim": "new write after phase close"},
                    }
                ],
            )

        envelope = replay.model_dump(by_alias=True, exclude_none=True)
        self.assertFalse(envelope["isError"])
        self.assertEqual(envelope["structuredContent"], first)
        self.assertEqual(len(envelope["content"]), 1)
        self.assertEqual(envelope["content"][0]["type"], "text")
        self.assertEqual(json.loads(envelope["content"][0]["text"]), first)
        rejected_envelope = rejected.model_dump(by_alias=True, exclude_none=True)
        self.assertTrue(rejected_envelope["isError"])
        self.assertEqual(
            rejected_envelope["structuredContent"]["error"], "phase is closed"
        )
        self.assertFalse(
            rejected_envelope["structuredContent"]["retry_allowed"]
        )

    def test_mcp_batch_validation_failure_preserves_exact_error(self) -> None:
        functions: dict[str, object] = {}

        class FakeMCP:
            def __init__(self, _name: str) -> None:
                pass

            def tool(self, *, name: str):
                def register(function):
                    functions[name] = function
                    return function

                return register

        with (
            mock.patch.object(server, "FastMCP", FakeMCP),
            mock.patch.object(server, "_context_rehydrate_preflight", return_value=None),
            mock.patch.object(server, "_reasoning_phase_preflight", return_value=None),
        ):
            server.build_mcp_app()
            batch_tool = functions["memory_append_batch"]
            assert callable(batch_tool)
            result = batch_tool(
                "checkpoint-error",
                [{"channel": "not-a-channel", "record": {"claim": "x"}}],
            )

        envelope = result.model_dump(by_alias=True, exclude_none=True)
        self.assertTrue(envelope["isError"])
        payload = envelope["structuredContent"]
        self.assertEqual(
            payload,
            {
                "schema_version": "rethlas_memory_checkpoint_failure_v1",
                "status": "preflight_failed",
                "category": "memory_checkpoint_validation",
                "error": (
                    "Unknown channel 'not-a-channel'. Allowed channels: "
                    "big_decisions, branch_states, counterexamples, events, "
                    "failed_paths, immediate_conclusions, proof_steps, "
                    "route_reviews, subgoals, targeted_verifications, "
                    "toy_examples, verification_reports"
                ),
                "retry_allowed": True,
                "checkpoint_committed": False,
            },
        )
        self.assertEqual(json.loads(envelope["content"][0]["text"]), payload)

    def test_mcp_batch_does_not_mislabel_post_preflight_value_error(self) -> None:
        functions: dict[str, object] = {}

        class FakeMCP:
            def __init__(self, _name: str) -> None:
                pass

            def tool(self, *, name: str):
                def register(function):
                    functions[name] = function
                    return function

                return register

        with (
            mock.patch.object(server, "FastMCP", FakeMCP),
            mock.patch.object(server, "_context_rehydrate_preflight", return_value=None),
            mock.patch.object(server, "_reasoning_phase_preflight", return_value=None),
        ):
            server.build_mcp_app()
            batch_tool = functions["memory_append_batch"]
            assert callable(batch_tool)
            with mock.patch.object(
                server,
                "memory_append_batch",
                side_effect=ValueError("post-preflight durability check failed"),
            ):
                with self.assertRaisesRegex(
                    ValueError, "post-preflight durability check failed"
                ):
                    batch_tool(
                        "checkpoint-error",
                        [{"channel": "proof_steps", "record": {"claim": "x"}}],
                    )

    def test_control_publication_receipts_use_commit_witness_and_replay_exactly(
        self,
    ) -> None:
        problem_id = "control-publication-time"
        prepared = {
            "review_id": "review_" + "a" * 32,
            "request_sha256": "1" * 64,
            "snapshot_sha256": "2" * 64,
            "state": "completed_pending_close",
        }
        with mock.patch.object(
            server,
            "_utc_now",
            return_value="2026-08-10T00:00:00+00:00",
        ):
            review_receipt = server._append_review_memory(
                problem_id=problem_id,
                body=prepared,
            )
        review_record = next(
            record
            for record in server._trusted_checkpoint_records(problem_id).values()
            if record["channel"] == "route_reviews"
        )
        replay = server._publication_receipt_for_existing(
            problem_id, review_record, prepared
        )
        checkpoint = server._validate_memory_batch_checkpoint(
            problem_id,
            server._batch_checkpoint_dir(problem_id)
            / f"{review_record['batch_id']}.json",
        )
        self.assertEqual(review_receipt, replay)
        self.assertEqual(
            review_receipt["timestamp_utc"], checkpoint["committed_at_utc"]
        )
        self.assertNotEqual(
            review_receipt["timestamp_utc"], checkpoint["timestamp_utc"]
        )

        targeted_body = {
            **prepared,
            "state": "official_published",
            "run_id": "run-1",
        }
        ticket = {"ticket_id": "claim_" + "b" * 32}
        targeted_receipt = server._append_targeted_result_memory(
            problem_id=problem_id,
            review_body=targeted_body,
            ticket=ticket,
            outcome_state="operational_blocked",
            verification_receipt=None,
            error_sha256="3" * 64,
        )
        found = server._find_targeted_result_memory(
            problem_id,
            review_id=prepared["review_id"],
            request_sha256=prepared["request_sha256"],
            snapshot_sha256=prepared["snapshot_sha256"],
        )
        self.assertIsNotNone(found)
        assert found is not None
        targeted_record, stored_body = found
        targeted_replay = server._targeted_publication_receipt_for_existing(
            problem_id, targeted_record, stored_body
        )
        targeted_checkpoint = server._validate_memory_batch_checkpoint(
            problem_id,
            server._batch_checkpoint_dir(problem_id)
            / f"{targeted_record['batch_id']}.json",
        )
        self.assertEqual(targeted_receipt, targeted_replay)
        self.assertEqual(
            targeted_receipt["timestamp_utc"],
            targeted_checkpoint["committed_at_utc"],
        )

    def test_memory_append_batch_hash_collision_rejects_different_items(self) -> None:
        problem_id = "simulated-hash-collision"
        first_items = [{"channel": "proof_steps", "record": {"claim": "original item"}}]
        different_items = [
            {"channel": "proof_steps", "record": {"claim": "different item"}}
        ]
        first = server.memory_append_batch(problem_id, first_items)

        with mock.patch.object(
            server,
            "_batch_id_for_items",
            return_value=first["batch_id"],
        ):
            with self.assertRaisesRegex(ValueError, "collides with different items"):
                server.memory_append_batch(problem_id, different_items)

        checkpoint_dir = server._batch_checkpoint_dir(problem_id)
        self.assertEqual(list(checkpoint_dir.glob(".*.tmp")), [])
        logical = server._load_memory_entries(problem_id)
        self.assertEqual(len(logical["proof_steps"]), 1)
        self.assertEqual(
            logical["proof_steps"][0]["item"]["record"],
            {"claim": "original item"},
        )

    def test_memory_append_batch_concurrent_exact_calls_publish_one_winner(
        self,
    ) -> None:
        items = [
            {
                "channel": "failed_paths",
                "record": {"obstruction": "shared concurrent checkpoint"},
            }
        ]
        worker_count = 4
        publish_barrier = threading.Barrier(worker_count)
        real_link = server.os.link

        def synchronized_link(
            source: str,
            destination: str,
            *,
            src_dir_fd: int,
            dst_dir_fd: int,
            follow_symlinks: bool,
        ) -> None:
            publish_barrier.wait(timeout=10)
            real_link(
                source,
                destination,
                src_dir_fd=src_dir_fd,
                dst_dir_fd=dst_dir_fd,
                follow_symlinks=follow_symlinks,
            )

        with mock.patch.object(server.os, "link", side_effect=synchronized_link):
            with ThreadPoolExecutor(max_workers=worker_count) as executor:
                responses = list(
                    executor.map(
                        lambda _: server.memory_append_batch("concurrent", items),
                        range(worker_count),
                    )
                )

        self.assertTrue(all(response == responses[0] for response in responses))
        checkpoint_dir = server._batch_checkpoint_dir("concurrent")
        self.assertEqual(len(list(checkpoint_dir.glob("batch_*.json"))), 1)
        self.assertEqual(list(checkpoint_dir.glob(".*.tmp")), [])
        logical = server._load_memory_entries("concurrent")
        self.assertEqual(len(logical["failed_paths"]), 1)
        self.assertEqual(len(logical["events"]), 1)

    def test_content_addressed_batch_rejects_record_body_tampering(self) -> None:
        receipt = server.memory_append_batch(
            "hash-binding",
            [
                {
                    "channel": "proof_steps",
                    "record": {"claim": "hash-bound checkpoint"},
                }
            ],
        )
        checkpoint = Path(receipt["checkpoint_path"])
        payload = json.loads(checkpoint.read_text(encoding="utf-8"))
        payload["records"][0]["record"]["claim"] = "tampered checkpoint"
        checkpoint.write_text(
            json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(ValueError, "not hash-bound"):
            server._load_memory_entries("hash-binding")

    def test_content_addressed_batch_rejects_timestamp_tampering(self) -> None:
        problem_id = "timestamp-binding"
        receipt = server.memory_append_batch(
            problem_id,
            [{"channel": "proof_steps", "record": {"claim": "time-bound"}}],
        )
        checkpoint = Path(receipt["checkpoint_path"])
        payload = json.loads(checkpoint.read_text(encoding="utf-8"))
        forged = "9999-12-31T23:59:59+00:00"
        payload["timestamp_utc"] = forged
        payload["records"][0]["timestamp_utc"] = forged
        payload["event"]["timestamp_utc"] = forged
        checkpoint.write_text(
            json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(ValueError, "not hash-bound"):
            server._load_memory_entries(problem_id)

    def test_content_addressed_batch_requires_canonical_utc_timestamp(self) -> None:
        problem_id = "canonical-timestamp"
        receipt = server.memory_append_batch(
            problem_id,
            [{"channel": "proof_steps", "record": {"claim": "UTC only"}}],
        )
        checkpoint = Path(receipt["checkpoint_path"])
        payload = json.loads(checkpoint.read_text(encoding="utf-8"))
        noncanonical = "2026-08-10T00:00:00Z"
        payload["timestamp_utc"] = noncanonical
        payload["records"][0]["timestamp_utc"] = noncanonical
        payload["event"]["timestamp_utc"] = noncanonical
        payload["checkpoint_sha256"] = server._memory_batch_checkpoint_sha256(payload)
        checkpoint.write_text(
            json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(ValueError, "canonical UTC timestamp"):
            server._load_memory_entries(problem_id)

    def test_memory_batch_strict_json_rejects_nested_duplicate_keys(self) -> None:
        problem_id = "duplicate-checkpoint-key"
        receipt = server.memory_append_batch(
            problem_id,
            [{"channel": "proof_steps", "record": {"claim": "original"}}],
        )
        checkpoint = Path(receipt["checkpoint_path"])
        raw = checkpoint.read_text(encoding="utf-8")
        tampered = raw.replace(
            '"record":{"claim":"original"}',
            '"record":{"claim":"first","claim":"second"}',
        )
        self.assertNotEqual(tampered, raw)
        checkpoint.write_text(tampered, encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "duplicate JSON key 'claim'"):
            server._load_memory_entries(problem_id)

    def test_memory_meta_strict_json_rejects_duplicate_keys(self) -> None:
        initialized = server.memory_init("duplicate-meta")
        meta_path = Path(initialized["meta_path"])
        meta_path.write_text(
            '{"problem_id":"duplicate-meta","problem_id":"other"}\n',
            encoding="utf-8",
        )

        with self.assertRaisesRegex(ValueError, "duplicate JSON key 'problem_id'"):
            server.memory_init("duplicate-meta")

    def test_legacy_v2_checkpoint_without_marker_remains_readable(self) -> None:
        problem_id = "legacy-v2-readable"
        server.memory_init(problem_id)
        checkpoint_dir = server._batch_checkpoint_dir(problem_id)
        server._ensure_memory_directory_durable(checkpoint_dir)
        items = [
            {
                "channel": "proof_steps",
                "record": {"claim": "legacy checkpoint remains visible"},
                "active": True,
                "supersedes": [],
            }
        ]
        encoded_items = json.dumps(items, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
        batch_id = server._batch_id_for_items(
            problem_id,
            encoded_items,
            schema=server.LEGACY_MEMORY_BATCH_SCHEMA,
        )
        timestamp = "2026-08-10T00:00:00+00:00"
        record = {
            "record_id": server._batch_record_id(batch_id, 0),
            "timestamp_utc": timestamp,
            "channel": "proof_steps",
            "active": True,
            "supersedes": [],
            "batch_id": batch_id,
            "record": items[0]["record"],
        }
        payload = {
            "schema": server.LEGACY_MEMORY_BATCH_SCHEMA,
            "batch_id": batch_id,
            "timestamp_utc": timestamp,
            "records": [record],
            "event": {
                "record_id": server._batch_event_id(batch_id),
                "timestamp_utc": timestamp,
                "event_type": "memory_append_batch",
                "batch_id": batch_id,
                "active": True,
                "supersedes": [],
                "appended_records": [
                    {"record_id": record["record_id"], "channel": "proof_steps"}
                ],
            },
        }
        payload["checkpoint_sha256"] = server._memory_batch_checkpoint_sha256(payload)
        checkpoint = checkpoint_dir / f"{batch_id}.json"
        checkpoint.write_text(
            json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )

        self.assertFalse(server._batch_commit_path(checkpoint).exists())
        trusted = server._trusted_checkpoint_records(problem_id)
        self.assertEqual(trusted[record["record_id"]]["timestamp_utc"], timestamp)
        self.assertEqual(trusted[record["record_id"]]["record"], items[0]["record"])
        with (
            mock.patch.object(
                server, "_released_memory_registry_configured", return_value=True
            ),
            mock.patch.object(
                server, "_memory_batch_registry_manifest", return_value={}
            ),
        ):
            self.assertEqual(server._trusted_checkpoint_records(problem_id), {})
            self.assertEqual(
                server._load_memory_entries(problem_id)["proof_steps"], []
            )

        validated = server._validate_memory_batch_checkpoint_data(
            problem_id, checkpoint
        )
        commit = server._publish_memory_batch_commit_once(
            checkpoint,
            validated,
            initial_cutoffs=None,
            publication_preflight=None,
        )
        fake_receipt = {
            "schema_version": server.MEMORY_BATCH_PUBLICATION_RECEIPT_SCHEMA,
            "state": "accepted",
            "run_id": "run-legacy-test",
            "problem_id": problem_id,
            "batch_id": batch_id,
            "checkpoint_sha256": validated["checkpoint_sha256"],
            "commit_sha256": commit["commit_sha256"],
            "publication_class": "reasoning_checkpoint",
            "accepted_at_utc": "2026-08-10T00:00:01+00:00",
            "accepted_at_monotonic": 1.0,
        }
        with (
            mock.patch.dict(
                os.environ,
                {server._REVIEW_RUN_ENV: "run-legacy-test"},
                clear=False,
            ),
            mock.patch.object(
                server, "_released_memory_registry_configured", return_value=True
            ),
            mock.patch.object(
                server,
                "_memory_batch_registry_manifest",
                return_value={batch_id: fake_receipt},
            ),
        ):
            with self.assertRaisesRegex(
                ValueError, "does not bind its artifact"
            ):
                server._validate_memory_batch_checkpoint(problem_id, checkpoint)

    def test_v2_checkpoint_rejects_canonical_non_sha_batch_id(self) -> None:
        problem_id = "non-sha-checkpoint"
        server.memory_init(problem_id)
        checkpoint_dir = server._batch_checkpoint_dir(problem_id)
        server._ensure_memory_directory_durable(checkpoint_dir)
        batch_id = "batch_not_a_sha"
        timestamp = "2026-08-10T00:00:00+00:00"
        record = {
            "record_id": "mem_non_sha",
            "timestamp_utc": timestamp,
            "channel": "proof_steps",
            "active": True,
            "supersedes": [],
            "batch_id": batch_id,
            "record": {"claim": "must not enter logical memory"},
        }
        payload = {
            "schema": server.LEGACY_MEMORY_BATCH_SCHEMA,
            "batch_id": batch_id,
            "timestamp_utc": timestamp,
            "records": [record],
            "event": {
                "record_id": "event_non_sha",
                "timestamp_utc": timestamp,
                "event_type": "memory_append_batch",
                "batch_id": batch_id,
                "active": True,
                "supersedes": [],
                "appended_records": [
                    {"record_id": record["record_id"], "channel": record["channel"]}
                ],
            },
        }
        payload["checkpoint_sha256"] = server._memory_batch_checkpoint_sha256(payload)
        checkpoint = checkpoint_dir / "batch_not_a_sha.json"
        checkpoint.write_text(
            json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(ValueError, "requires a full-SHA"):
            server._load_memory_entries(problem_id)

        self.assertTrue(checkpoint.is_file())

    def test_unreleased_v1_checkpoint_fails_closed(self) -> None:
        problem_id = "old-v1-checkpoint"
        receipt = server.memory_append_batch(
            problem_id,
            [{"channel": "proof_steps", "record": {"claim": "v2 winner"}}],
        )
        checkpoint = Path(receipt["checkpoint_path"])
        payload = json.loads(checkpoint.read_text(encoding="utf-8"))
        payload["schema"] = "rethlas_memory_batch_v1"
        payload.pop("checkpoint_sha256")
        checkpoint.write_text(
            json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(ValueError, "invalid envelope"):
            server._load_memory_entries(problem_id)

    def test_committed_batch_supersedes_legacy_record_as_one_logical_update(
        self,
    ) -> None:
        prior = server.memory_append(
            "atomic-success",
            "proof_steps",
            {"claim": "batchneedle old route"},
        )
        batch = server.memory_append_batch(
            "atomic-success",
            [
                {
                    "channel": "proof_steps",
                    "record": {"claim": "batchneedle replacement route"},
                    "supersedes": [prior["record_id"]],
                }
            ],
        )

        current = server.memory_search(
            "atomic-success",
            "batchneedle",
            channels=["proof_steps"],
            max_chars=LARGE_SEARCH_BUDGET,
        )
        current_items = current["results_by_channel"]["proof_steps"]["results"]
        self.assertEqual(len(current_items), 1)
        self.assertEqual(
            current_items[0]["item"]["record_id"], batch["records"][0]["record_id"]
        )

        history = server.memory_search(
            "atomic-success",
            "batchneedle",
            channels=["proof_steps"],
            max_chars=LARGE_SEARCH_BUDGET,
            include_inactive=True,
        )
        history_items = history["results_by_channel"]["proof_steps"]["results"]
        active_by_id = {
            result["item"]["record_id"]: result["item"]["active"]
            for result in history_items
        }
        self.assertEqual(
            active_by_id,
            {prior["record_id"]: False, batch["records"][0]["record_id"]: True},
        )

    def test_orphan_batch_temp_file_is_not_logically_visible(self) -> None:
        server.memory_init("orphan-temp")
        checkpoint_dir = server._batch_checkpoint_dir("orphan-temp")
        checkpoint_dir.mkdir(parents=True)
        orphan = checkpoint_dir / ".batch_deadbeef.json.crash.tmp"
        orphan.write_text('{"record":"must remain invisible"}\n', encoding="utf-8")

        logical = server._load_memory_entries("orphan-temp")
        self.assertTrue(all(not entries for entries in logical.values()))

    def test_memory_append_batch_validates_every_item_before_writing(self) -> None:
        with self.assertRaisesRegex(ValueError, r"items\[1\]\.record"):
            server.memory_append_batch(
                "invalid-batch",
                [
                    {"channel": "proof_steps", "record": {"text": "valid"}},
                    {"channel": "failed_paths", "record": "not-an-object"},
                ],
            )
        self.assertFalse((server.MEMORY_ROOT / "invalid-batch").exists())

        with self.assertRaisesRegex(ValueError, "strict JSON data"):
            server.memory_append_batch(
                "invalid-json",
                [
                    {
                        "channel": "proof_steps",
                        "record": {"score": float("nan")},
                    }
                ],
            )
        self.assertFalse((server.MEMORY_ROOT / "invalid-json").exists())

        with self.assertRaisesRegex(ValueError, "at most"):
            server.memory_append_batch(
                "too-many",
                [
                    {"channel": "events", "record": {"index": index}}
                    for index in range(server.MAX_MEMORY_BATCH_RECORDS + 1)
                ],
            )
        self.assertFalse((server.MEMORY_ROOT / "too-many").exists())

    def test_active_and_supersedes_filter_stale_records(self) -> None:
        first = server.memory_append(
            "sample",
            "proof_steps",
            {"text": "sharedneedle first"},
        )
        replacement = server.memory_append(
            "sample",
            "proof_steps",
            {"text": "sharedneedle replacement"},
            supersedes=[first["record_id"]],
        )
        inactive = server.memory_append(
            "sample",
            "proof_steps",
            {"text": "sharedneedle inactive"},
            active=False,
        )

        current = server.memory_search(
            "sample",
            "sharedneedle",
            channels=["proof_steps"],
            max_chars=LARGE_SEARCH_BUDGET,
        )
        current_results = current["results_by_channel"]["proof_steps"]["results"]
        self.assertEqual(current["corpus_count"], 1)
        self.assertEqual(
            [result["item"]["record_id"] for result in current_results],
            [replacement["record_id"]],
        )

        history = server.memory_search(
            "sample",
            "sharedneedle",
            channels=["proof_steps"],
            max_chars=LARGE_SEARCH_BUDGET,
            include_inactive=True,
        )
        history_results = history["results_by_channel"]["proof_steps"]["results"]
        self.assertEqual(history["corpus_count"], 3)
        active_by_id = {
            result["item"]["record_id"]: result["item"]["active"]
            for result in history_results
        }
        self.assertEqual(
            active_by_id,
            {
                first["record_id"]: False,
                replacement["record_id"]: True,
                inactive["record_id"]: False,
            },
        )
        stale_item = next(
            result["item"]
            for result in history_results
            if result["item"]["record_id"] == first["record_id"]
        )
        self.assertEqual(stale_item["superseded_by"], [replacement["record_id"]])

    def test_legacy_records_get_stable_ids_and_can_be_superseded(self) -> None:
        server.memory_init("legacy")
        server._append_jsonl(
            server._channel_path("legacy", "counterexamples"),
            {
                "timestamp_utc": "2025-01-01T00:00:00+00:00",
                "channel": "counterexamples",
                "record": {"text": "legacyneedle counterexample"},
            },
        )

        first_search = server.memory_search(
            "legacy",
            "legacyneedle",
            channels=["counterexamples"],
            max_chars=LARGE_SEARCH_BUDGET,
        )
        legacy_item = first_search["results_by_channel"]["counterexamples"]["results"][
            0
        ]["item"]
        legacy_id = legacy_item["record_id"]
        self.assertTrue(legacy_id.startswith("legacy_"))
        self.assertTrue(legacy_item["active"])
        self.assertEqual(legacy_item["supersedes"], [])

        repeated_search = server.memory_search(
            "legacy",
            "legacyneedle",
            channels=["counterexamples"],
            max_chars=LARGE_SEARCH_BUDGET,
        )
        self.assertEqual(
            repeated_search["results_by_channel"]["counterexamples"]["results"][0][
                "item"
            ]["record_id"],
            legacy_id,
        )

        replacement = server.memory_append(
            "legacy",
            "counterexamples",
            {"text": "legacyneedle corrected counterexample"},
            supersedes=[legacy_id],
        )
        current = server.memory_search(
            "legacy",
            "legacyneedle",
            channels=["counterexamples"],
            max_chars=LARGE_SEARCH_BUDGET,
        )
        self.assertEqual(
            current["results_by_channel"]["counterexamples"]["results"][0]["item"][
                "record_id"
            ],
            replacement["record_id"],
        )
        self.assertEqual(current["corpus_count"], 1)

    def test_search_ranks_by_relevance_then_breaks_ties_by_recency(self) -> None:
        server.memory_init("relevance-ordering")
        channel_path = server._channel_path(
            "relevance-ordering", "immediate_conclusions"
        )
        server._append_jsonl(
            channel_path,
            {
                "record_id": "highly-relevant-old",
                "timestamp_utc": "2025-01-01T00:00:00+00:00",
                "channel": "immediate_conclusions",
                "active": True,
                "supersedes": [],
                "record": {"text": "alpha beta gamma delta"},
            },
        )
        server._append_jsonl(
            channel_path,
            {
                "record_id": "weakly-relevant-new",
                "timestamp_utc": "2026-01-01T00:00:00+00:00",
                "channel": "immediate_conclusions",
                "active": True,
                "supersedes": [],
                "record": {"text": "alpha"},
            },
        )

        response = server.memory_search(
            "relevance-ordering",
            "alpha beta gamma delta",
            channels=["immediate_conclusions"],
            max_chars=LARGE_SEARCH_BUDGET,
        )
        relevance_results = response["results_by_channel"]["immediate_conclusions"][
            "results"
        ]
        ids = [result["item"]["record_id"] for result in relevance_results]
        self.assertEqual(ids, ["highly-relevant-old", "weakly-relevant-new"])
        self.assertGreater(relevance_results[0]["score"], relevance_results[1]["score"])

        server.memory_init("tie-ordering")
        tie_path = server._channel_path("tie-ordering", "immediate_conclusions")
        for record_id, timestamp in (
            ("equal-old", "2025-01-01T00:00:00+00:00"),
            ("equal-new", "2026-01-01T00:00:00+00:00"),
        ):
            server._append_jsonl(
                tie_path,
                {
                    "record_id": record_id,
                    "timestamp_utc": timestamp,
                    "channel": "immediate_conclusions",
                    "active": True,
                    "supersedes": [],
                    "record": {"text": "tieprobe identical"},
                },
            )

        tied = server.memory_search(
            "tie-ordering",
            "tieprobe",
            channels=["immediate_conclusions"],
            max_chars=LARGE_SEARCH_BUDGET,
        )
        tied_results = tied["results_by_channel"]["immediate_conclusions"]["results"]
        tied_ids = [result["item"]["record_id"] for result in tied_results]
        self.assertEqual(tied_ids, ["equal-new", "equal-old"])
        self.assertAlmostEqual(tied_results[0]["score"], tied_results[1]["score"])

    def test_search_budget_returns_only_whole_records_and_reports_omissions(
        self,
    ) -> None:
        for index in range(3):
            server.memory_append(
                "budget",
                "proof_steps",
                {"text": f"budgetneedle record {index}", "payload": "x" * 50},
            )
        server.memory_append(
            "budget",
            "proof_steps",
            {"text": "unrelated corpus entry"},
        )

        unbounded = server.memory_search(
            "budget",
            "budgetneedle",
            channels=["proof_steps"],
            max_chars=LARGE_SEARCH_BUDGET,
        )
        unbounded_channel = unbounded["results_by_channel"]["proof_steps"]
        self.assertEqual(unbounded["corpus_count"], 4)
        self.assertEqual(unbounded["matched_count"], 3)
        self.assertTrue(unbounded["complete"])
        first_result = unbounded_channel["results"][0]
        first_chars = server._compact_json_chars(first_result)

        bounded = server.memory_search(
            "budget",
            "budgetneedle",
            channels=["proof_steps"],
            max_chars=first_chars,
        )
        bounded_channel = bounded["results_by_channel"]["proof_steps"]
        self.assertEqual(bounded["count"], 1)
        self.assertEqual(bounded["returned_chars"], first_chars)
        self.assertFalse(bounded["complete"])
        self.assertTrue(bounded["truncated"])
        self.assertEqual(bounded["omitted_count"], 2)
        self.assertEqual(len(bounded["omitted_ids"]), 2)
        self.assertEqual(bounded_channel["results"][0], first_result)
        self.assertEqual(bounded_channel["returned_chars"], first_chars)
        self.assertFalse(bounded_channel["complete"])

        no_room = server.memory_search(
            "budget",
            "budgetneedle",
            channels=["proof_steps"],
            max_chars=1,
        )
        self.assertEqual(no_room["count"], 0)
        self.assertEqual(no_room["returned_chars"], 0)
        self.assertEqual(len(no_room["omitted_ids"]), 3)

        limited = server.memory_search(
            "budget",
            "budgetneedle",
            channels=["proof_steps"],
            limit_per_channel=1,
            max_chars=LARGE_SEARCH_BUDGET,
        )
        self.assertEqual(limited["count"], 1)
        self.assertTrue(limited["truncated"])
        self.assertFalse(limited["complete"])
        self.assertEqual(limited["omitted_count"], 2)
        self.assertEqual(len(limited["omitted_ids"]), 2)

    def test_search_hard_caps_the_complete_utf8_response_envelope(self) -> None:
        for index in range(4):
            server.memory_append(
                "response-envelope",
                "proof_steps",
                {
                    "text": f"envelopeneedle record {index}",
                    "payload": "x" * 15_000,
                },
            )

        response = server.memory_search(
            "response-envelope",
            "envelopeneedle",
            channels=["proof_steps"],
            max_chars=64_000,
        )

        self.assertTrue(response["response_limited"])
        self.assertEqual(
            response["response_max_utf8_bytes"],
            server.MAX_MEMORY_SEARCH_RESPONSE_UTF8_BYTES,
        )
        self.assertLessEqual(
            server._memory_search_response_utf8_bytes(response),
            server.MAX_MEMORY_SEARCH_RESPONSE_UTF8_BYTES,
        )
        self.assertLess(response["count"], 4)
        self.assertTrue(response["truncated"])
        self.assertEqual(response["omitted_count"], 4 - response["count"])
        returned = response["results_by_channel"]["proof_steps"]["results"]
        self.assertEqual(len(returned), response["count"])
        self.assertTrue(
            all(
                result["item"]["record"]["payload"] == "x" * 15_000
                for result in returned
            )
        )

    def test_search_response_cap_counts_utf8_bytes_not_code_points(self) -> None:
        server.memory_append(
            "response-unicode",
            "proof_steps",
            {"text": "unicodeneedle", "payload": "😀" * 20_000},
        )

        response = server.memory_search(
            "response-unicode",
            "unicodeneedle",
            channels=["proof_steps"],
            max_chars=64_000,
        )

        self.assertTrue(response["response_limited"])
        self.assertEqual(response["count"], 0)
        self.assertLessEqual(
            server._memory_search_response_utf8_bytes(response),
            server.MAX_MEMORY_SEARCH_RESPONSE_UTF8_BYTES,
        )

    def test_memory_options_are_validated_before_writes(self) -> None:
        with self.assertRaisesRegex(ValueError, "return_mode"):
            server.memory_append(
                "invalid",
                "proof_steps",
                {"text": "not written"},
                return_mode="everything",
            )
        self.assertFalse((server.MEMORY_ROOT / "invalid").exists())

        with self.assertRaisesRegex(ValueError, "supersedes"):
            server.memory_append(
                "invalid",
                "proof_steps",
                {"text": "not written"},
                supersedes=[""],
            )
        with self.assertRaisesRegex(ValueError, "max_chars"):
            server.memory_search("invalid", "query", max_chars=-1)
        with self.assertRaisesRegex(ValueError, "MAX_MEMORY_SEARCH_QUERY_UTF8_BYTES"):
            server.memory_search(
                "invalid",
                "q" * (server.MAX_MEMORY_SEARCH_QUERY_UTF8_BYTES + 1),
            )

    def test_omitted_id_metadata_is_bounded(self) -> None:
        for index in range(6):
            server.memory_append(
                "many-omissions",
                "proof_steps",
                {"text": f"needle omitted record {index}"},
            )
        original_limit = server.MAX_OMITTED_IDS
        server.MAX_OMITTED_IDS = 2
        try:
            response = server.memory_search(
                "many-omissions",
                "needle",
                channels=["proof_steps"],
                max_chars=1,
            )
        finally:
            server.MAX_OMITTED_IDS = original_limit

        channel = response["results_by_channel"]["proof_steps"]
        self.assertEqual(response["omitted_count"], 6)
        self.assertEqual(len(response["omitted_ids"]), 2)
        self.assertFalse(response["omitted_ids_complete"])
        self.assertEqual(len(channel["omitted_ids"]), 2)
        self.assertFalse(channel["omitted_ids_complete"])

    def test_omitted_id_cap_boundaries_are_explicit(self) -> None:
        original_limit = server.MAX_OMITTED_IDS
        server.MAX_OMITTED_IDS = 2
        try:
            for omitted_count in (1, 2, 3):
                problem_id = f"omission-boundary-{omitted_count}"
                for index in range(omitted_count):
                    server.memory_append(
                        problem_id,
                        "proof_steps",
                        {"text": f"boundaryneedle record {index}"},
                    )
                response = server.memory_search(
                    problem_id,
                    "boundaryneedle",
                    channels=["proof_steps"],
                    max_chars=1,
                )
                channel = response["results_by_channel"]["proof_steps"]
                expected_listed = min(omitted_count, 2)
                self.assertEqual(response["omitted_count"], omitted_count)
                self.assertEqual(len(response["omitted_ids"]), expected_listed)
                self.assertEqual(response["omitted_ids_complete"], omitted_count <= 2)
                self.assertEqual(channel["omitted_count"], omitted_count)
                self.assertEqual(len(channel["omitted_ids"]), expected_listed)
                self.assertEqual(channel["omitted_ids_complete"], omitted_count <= 2)
        finally:
            server.MAX_OMITTED_IDS = original_limit


if __name__ == "__main__":
    unittest.main()
