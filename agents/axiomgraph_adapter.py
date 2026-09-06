"""Explicit graph-native Relay controller; never a legacy-run importer.

This optional profile uses AxiomGraph's configured source service for every
effect. Importing it neither modifies the existing Git publication capability
nor enables a Claude, legacy or hot-join runner. Hosts must separately enroll a
fresh graph-native problem/run; terminal prose and manual Pro files cannot do so.
"""

from __future__ import annotations

import importlib


class RelayGraphError(PermissionError):
    pass


class RelayGraphExecutor:
    """Deployment-local facade over the single source gate, with no dual writes."""

    def __init__(self, source, graph_id, *, run_id, credential):
        self._rt = importlib.import_module("axiomgraph_runtime")
        self._ag = importlib.import_module("axiomgraph_contract")
        if (self._ag.CONTRACT_VERSION != "axiomgraph_contract_v1"
                or self._ag.SCHEMA_BUNDLE_DIGEST != "sha256:3b188496f03ba208565ab5d8862e745be1cc58bcca2cbbdf084148e8e8e71d60"
                or self._rt.TRANSFER_CAPABILITY != "stopped_unsolved_transfer_v1"
                or self._rt.GRAPH_RUN_PROFILE != "axiomgraph_gated_run_v1"):
            raise RelayGraphError("unsupported graph-native transfer contract")
        if not isinstance(source, self._rt.TransferAuthorityService):
            raise TypeError("Relay graph execution requires a configured transfer source")
        self._source, self._graph_id, self._run_id, self._credential = source, graph_id, run_id, credential
        self.observe()

    def __reduce__(self):
        raise TypeError("Relay graph executors contain deployment-local credentials")

    def observe(self):
        observed = self._source.observe(self._graph_id, credential=self._credential)
        run = self._source.transfer_state(self._graph_id, credential=self._credential)
        if observed.pin.controller.principal_id != "axiomrelay" or run.run_id != self._run_id:
            raise RelayGraphError("Relay does not control this exact graph run")
        return observed

    def source_profile(self):
        self.observe()
        return {"capability": self._rt.TRANSFER_CAPABILITY, "profile": self._rt.GRAPH_RUN_PROFILE,
                "graph_id": self._graph_id, "run_id": self._run_id,
                "schema_bundle_digest": self._ag.SCHEMA_BUNDLE_DIGEST,
                "legacy_import": False, "publication_git_profile": False}

    def root_context(self, observed):
        if observed != self.observe():
            raise RelayGraphError("stale source observation")
        return self._ag.canonical_bytes({
            "format": "axiomrelay_graph_root_context_v1", "run_id": self._run_id,
            "basis": observed.pin.to_payload(),
            "bundle": self._ag.canonical.parse_canonical_json(self._ag.dump_graph_bundle(observed.bundle)),
            "semantic_reset_digest": self._rt.semantic_reset_digest(observed.bundle),
            "legacy_memory_write": False, "legacy_publication_write": False,
        })

    def _graph(self, observed):
        if type(observed) is not self._rt.SourceObservation or observed.pin.graph_id != self._graph_id:
            raise RelayGraphError("operation belongs to a different graph")

    def reserve(self, packet):
        self._graph(packet.basis)
        return self._source.reserve_work(packet, credential=self._credential)

    def advance(self, work_id, action, observed, *, operation_id):
        self._graph(observed)
        return self._source.advance_work(work_id, action, observed, operation_id=operation_id, credential=self._credential)

    def append_claims(self, bundle, observed, *, operation_id):
        self._graph(observed)
        if bundle.graph_id != self._graph_id:
            raise RelayGraphError("append belongs to another graph")
        return self._source.append(bundle, expected=observed.pin, operation_id=operation_id, credential=self._credential)

    def admit_derivation(self, packet, *, receipt_digest):
        self._graph(packet.basis)
        return self._source.admit(packet, receipt_digest=receipt_digest, credential=self._credential)

    def admit_cohort(self, observed, alternatives, *, operation_id):
        self._graph(observed)
        return self._source.admit_cohort(observed, alternatives, operation_id=operation_id, credential=self._credential)

    def finish_unsolved(self, observed, reports, *, operation_id):
        self._graph(observed)
        return self._source.finish_unsolved(observed, reports, operation_id=operation_id, credential=self._credential)

    def prepare_export(self, observed, *, operation_id):
        self._graph(observed)
        return self._source.prepare_export(observed, operation_id=operation_id, credential=self._credential)

    def commit_transfer(self, observed, *, export_digest, destination_receipt_digest, operation_id):
        self._graph(observed)
        return self._source.commit_transfer(observed, export_digest=export_digest,
                                            destination_receipt_digest=destination_receipt_digest,
                                            operation_id=operation_id, credential=self._credential)

    def lookup(self, operation_id):
        # Historical lookup/replay remains legal after the source fences Relay.
        return self._source.lookup(operation_id, credential=self._credential)


__all__ = ["RelayGraphError", "RelayGraphExecutor"]
