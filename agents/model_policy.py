#!/usr/bin/env python3
"""Pure, zero-model resolver for AxiomRelay role model policies.

This module implements the first control-plane slice of MODEL_POLICY.md. It
does not launch Root, Generator, or Verifier actors and does not mutate runtime
state. Runtime integration remains gated by the milestones in the design.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping


SCHEMA_VERSION = "rethlas_model_policy_v1"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
PROBLEM_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,255}$")
MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@\[\]-]{0,127}$")
PROFILES = {"compatible", "balanced", "max_diversity", "economy", "custom"}
EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}
PROVIDERS = {"openai", "anthropic", "vertex", "bedrock", "foundry"}
ADAPTERS = {"codex_cli", "claude_cli"}
MAX_POLICY_BYTES = 131_072


class ModelPolicyError(RuntimeError):
    """The requested policy is invalid or not implemented."""


@dataclass(frozen=True)
class Capability:
    adapter: str
    provider: str
    model: str
    roles: frozenset[str]
    efforts: frozenset[str]
    session_modes: frozenset[str]
    implemented: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "adapter": self.adapter,
            "provider": self.provider,
            "model": self.model,
            "roles": sorted(self.roles),
            "efforts": sorted(self.efforts),
            "session_modes": sorted(self.session_modes),
            "implemented": self.implemented,
        }


_CODEX_EFFORTS = frozenset({"low", "medium", "high", "xhigh", "max", "ultra"})
_LUNA_EFFORTS = frozenset({"low", "medium", "high", "xhigh", "max"})
_ASTRA_EFFORTS = frozenset({"low", "medium", "high", "xhigh", "max"})
DEFAULT_CODEX_MODEL = "gpt-6-astra"
MAX_DIVERSITY_CODEX_MODEL = DEFAULT_CODEX_MODEL


def _capabilities() -> tuple[Capability, ...]:
    values: list[Capability] = []
    for model, efforts in (
        (MAX_DIVERSITY_CODEX_MODEL, _ASTRA_EFFORTS),
        ("gpt-5.6-terra", _CODEX_EFFORTS),
        ("gpt-5.6-luna", _LUNA_EFFORTS),
    ):
        values.append(
            Capability(
                adapter="codex_cli",
                provider="openai",
                model=model,
                roles=frozenset({"generator", "verifier"}),
                efforts=efforts,
                session_modes=frozenset({"isolated", "cold"}),
                implemented=True,
            )
        )
        values.append(
            Capability(
                adapter="codex_cli",
                provider="openai",
                model=model,
                roles=frozenset({"root"}),
                efforts=efforts,
                session_modes=frozenset({"persistent_logical_root"}),
                implemented=False,
            )
        )
    for provider in ("anthropic", "vertex", "bedrock", "foundry"):
        for model in ("claude-opus-5", "claude-fable-5"):
            values.append(
                Capability(
                    adapter="claude_cli",
                    provider=provider,
                    model=model,
                    roles=frozenset({"root"}),
                    efforts=frozenset({"max"}),
                    session_modes=frozenset({"persistent_logical_root"}),
                    implemented=True,
                )
            )
            values.append(
                Capability(
                    adapter="claude_cli",
                    provider=provider,
                    model=model,
                    roles=frozenset({"verifier"}),
                    efforts=frozenset({"max"}),
                    session_modes=frozenset({"cold"}),
                    implemented=True,
                )
            )
    return tuple(values)


CAPABILITIES = _capabilities()


def canonical_json(value: object) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def canonical_bytes(value: object) -> bytes:
    return (canonical_json(value) + "\n").encode("utf-8")


def policy_sha256(policy: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_bytes(policy)).hexdigest()


def _exact_object(
    value: object, expected: Iterable[str], *, label: str
) -> dict[str, Any]:
    expected_keys = set(expected)
    if not isinstance(value, dict) or set(value) != expected_keys:
        raise ModelPolicyError(f"{label} has an unsupported shape")
    return dict(value)


def _safe_problem_id(value: object) -> str:
    if not isinstance(value, str) or PROBLEM_ID_RE.fullmatch(value) is None:
        raise ModelPolicyError("problem_id is invalid")
    if any(part in {"", ".", ".."} for part in value.split("/")):
        raise ModelPolicyError("problem_id contains an unsafe component")
    return value


def _sha256(value: object, *, label: str) -> str:
    if not isinstance(value, str) or SHA256_RE.fullmatch(value) is None:
        raise ModelPolicyError(f"{label} must be a lowercase SHA-256")
    return value


def _text(value: object, *, label: str, allowed: set[str] | None = None) -> str:
    if not isinstance(value, str) or not value:
        raise ModelPolicyError(f"{label} must be a nonempty string")
    if allowed is not None and value not in allowed:
        raise ModelPolicyError(f"{label} is unsupported")
    return value


def _capability(
    *,
    adapter: str,
    provider: str,
    model: str,
    role: str,
    effort: str,
    session_mode: str,
) -> Capability:
    matches = [
        item
        for item in CAPABILITIES
        if item.adapter == adapter
        and item.provider == provider
        and item.model == model
        and role in item.roles
        and effort in item.efforts
        and session_mode in item.session_modes
    ]
    if len(matches) != 1:
        raise ModelPolicyError(
            f"no capability matches {role}={adapter}/{provider}/{model}/{effort}/{session_mode}"
        )
    capability = matches[0]
    if not capability.implemented:
        raise ModelPolicyError(
            f"capability is designed but not implemented for role={role}: "
            f"{adapter}/{provider}/{model}"
        )
    return capability


def _validate_root(value: object) -> dict[str, Any]:
    root = _exact_object(
        value,
        {"adapter", "provider", "model", "effort", "memory_mode"},
        label="root",
    )
    adapter = _text(root["adapter"], label="root.adapter", allowed=ADAPTERS)
    provider = _text(root["provider"], label="root.provider", allowed=PROVIDERS)
    model = _text(root["model"], label="root.model")
    effort = _text(root["effort"], label="root.effort", allowed=EFFORTS)
    if MODEL_RE.fullmatch(model) is None:
        raise ModelPolicyError("root.model is invalid")
    if root["memory_mode"] != "persistent_logical_root":
        raise ModelPolicyError("root.memory_mode must be persistent_logical_root")
    _capability(
        adapter=adapter,
        provider=provider,
        model=model,
        role="root",
        effort=effort,
        session_mode="persistent_logical_root",
    )
    return root


def _validate_generator(value: object) -> dict[str, Any]:
    generator = _exact_object(
        value,
        {
            "adapter",
            "provider",
            "model",
            "effort",
            "lane_policy",
            "max_live_paid_lanes",
            "session_mode",
        },
        label="generator",
    )
    adapter = _text(
        generator["adapter"], label="generator.adapter", allowed=ADAPTERS
    )
    provider = _text(
        generator["provider"], label="generator.provider", allowed=PROVIDERS
    )
    model = _text(generator["model"], label="generator.model")
    effort = _text(
        generator["effort"], label="generator.effort", allowed=EFFORTS
    )
    if MODEL_RE.fullmatch(model) is None:
        raise ModelPolicyError("generator.model is invalid")
    if generator["lane_policy"] != "uniform":
        raise ModelPolicyError("generator.lane_policy must be uniform")
    if (
        isinstance(generator["max_live_paid_lanes"], bool)
        or generator["max_live_paid_lanes"] != 3
    ):
        raise ModelPolicyError("generator.max_live_paid_lanes must be exactly 3")
    if generator["session_mode"] != "isolated":
        raise ModelPolicyError("generator.session_mode must be isolated")
    _capability(
        adapter=adapter,
        provider=provider,
        model=model,
        role="generator",
        effort=effort,
        session_mode="isolated",
    )
    return generator


def _validate_verifier_pass(value: object, *, index: int) -> dict[str, Any]:
    item = _exact_object(
        value,
        {
            "pass_index",
            "role",
            "adapter",
            "provider",
            "model",
            "launch_model",
            "effort",
            "session_mode",
        },
        label=f"verifier.passes[{index - 1}]",
    )
    expected_role = "primary" if index == 1 else "adversarial_full_claim_audit"
    if (
        isinstance(item["pass_index"], bool)
        or item["pass_index"] != index
        or item["role"] != expected_role
    ):
        raise ModelPolicyError(f"verifier pass {index} identity is invalid")
    adapter = _text(
        item["adapter"], label=f"verifier pass {index} adapter", allowed=ADAPTERS
    )
    provider = _text(
        item["provider"], label=f"verifier pass {index} provider", allowed=PROVIDERS
    )
    model = _text(item["model"], label=f"verifier pass {index} model")
    launch_model = _text(
        item["launch_model"], label=f"verifier pass {index} launch_model"
    )
    effort = _text(
        item["effort"], label=f"verifier pass {index} effort", allowed=EFFORTS
    )
    if MODEL_RE.fullmatch(model) is None:
        raise ModelPolicyError(f"verifier pass {index} model is invalid")
    if MODEL_RE.fullmatch(launch_model) is None:
        raise ModelPolicyError(f"verifier pass {index} launch_model is invalid")
    if item["session_mode"] != "cold":
        raise ModelPolicyError(f"verifier pass {index} must be cold")
    _capability(
        adapter=adapter,
        provider=provider,
        model=model,
        role="verifier",
        effort=effort,
        session_mode="cold",
    )
    return item


def _validate_verifier(value: object, *, root: Mapping[str, Any]) -> dict[str, Any]:
    verifier = _exact_object(
        value,
        {
            "quorum",
            "passes",
            "require_distinct_models",
            "require_distinct_providers",
            "require_adversarial_distinct_from_root",
            "automatic_tiebreaker",
        },
        label="verifier",
    )
    if isinstance(verifier["quorum"], bool) or verifier["quorum"] != 2:
        raise ModelPolicyError("verifier.quorum must be exactly 2")
    passes = verifier["passes"]
    if not isinstance(passes, list) or len(passes) != 2:
        raise ModelPolicyError("verifier.passes must contain exactly two passes")
    normalized_passes = [
        _validate_verifier_pass(passes[0], index=1),
        _validate_verifier_pass(passes[1], index=2),
    ]
    for field in (
        "require_distinct_models",
        "require_distinct_providers",
        "require_adversarial_distinct_from_root",
        "automatic_tiebreaker",
    ):
        if not isinstance(verifier[field], bool):
            raise ModelPolicyError(f"verifier.{field} must be boolean")
    if verifier["automatic_tiebreaker"] is not False:
        raise ModelPolicyError("automatic verifier tiebreaker is forbidden")
    primary, adversarial = normalized_passes
    if verifier["require_distinct_models"] and primary["model"] == adversarial["model"]:
        raise ModelPolicyError("verifier policy requires distinct models")
    if (
        verifier["require_distinct_providers"]
        and primary["provider"] == adversarial["provider"]
    ):
        raise ModelPolicyError("verifier policy requires distinct providers")
    if (
        verifier["require_adversarial_distinct_from_root"]
        and root["model"] == adversarial["model"]
    ):
        raise ModelPolicyError("adversarial verifier must differ from the Root model")
    return {**verifier, "passes": normalized_passes}


def validate_policy(value: object) -> dict[str, Any]:
    policy = _exact_object(
        value,
        {
            "schema_version",
            "problem_id",
            "statement_sha256",
            "revision",
            "parent_policy_sha256",
            "profile",
            "root",
            "generator",
            "verifier",
            "fallback_policy",
            "selection_authority",
        },
        label="model policy",
    )
    if policy["schema_version"] != SCHEMA_VERSION:
        raise ModelPolicyError("model policy schema version is unsupported")
    policy["problem_id"] = _safe_problem_id(policy["problem_id"])
    policy["statement_sha256"] = _sha256(
        policy["statement_sha256"], label="statement_sha256"
    )
    if isinstance(policy["revision"], bool) or not isinstance(policy["revision"], int):
        raise ModelPolicyError("model policy revision must be an integer")
    if policy["revision"] < 1:
        raise ModelPolicyError("model policy revision must be positive")
    if policy["parent_policy_sha256"] is not None:
        policy["parent_policy_sha256"] = _sha256(
            policy["parent_policy_sha256"], label="parent_policy_sha256"
        )
    if policy["revision"] == 1 and policy["parent_policy_sha256"] is not None:
        raise ModelPolicyError("initial model policy may not name a parent")
    if policy["revision"] > 1 and policy["parent_policy_sha256"] is None:
        raise ModelPolicyError("successor model policy requires a parent digest")
    policy["profile"] = _text(
        policy["profile"], label="profile", allowed=PROFILES
    )
    if policy["fallback_policy"] != "forbid":
        raise ModelPolicyError("model fallback policy must be forbid")
    if policy["selection_authority"] != "owner_wrapper":
        raise ModelPolicyError("model selection authority must be owner_wrapper")
    root = _validate_root(policy["root"])
    generator = _validate_generator(policy["generator"])
    verifier = _validate_verifier(policy["verifier"], root=root)
    normalized = {
        **policy,
        "root": root,
        "generator": generator,
        "verifier": verifier,
    }
    if len(canonical_bytes(normalized)) > MAX_POLICY_BYTES:
        raise ModelPolicyError("model policy exceeds its byte cap")
    return normalized


def _root_actor(
    *, adapter: str, provider: str, model: str, effort: str
) -> dict[str, Any]:
    return {
        "adapter": adapter,
        "provider": provider,
        "model": model,
        "effort": effort,
        "memory_mode": "persistent_logical_root",
    }


def _codex_actor(
    *, model: str, effort: str, role: str, pass_index: int | None = None
) -> dict[str, Any]:
    if role == "generator":
        return {
            "adapter": "codex_cli",
            "provider": "openai",
            "model": model,
            "effort": effort,
            "lane_policy": "uniform",
            "max_live_paid_lanes": 3,
            "session_mode": "isolated",
        }
    assert pass_index in {1, 2}
    return {
        "pass_index": pass_index,
        "role": "primary" if pass_index == 1 else "adversarial_full_claim_audit",
        "adapter": "codex_cli",
        "provider": "openai",
        "model": model,
        "launch_model": model,
        "effort": effort,
        "session_mode": "cold",
    }


def _secondary_codex_model(root_model: str) -> str:
    for candidate in ("gpt-5.6-terra", "gpt-5.6-luna"):
        if candidate != root_model:
            return candidate
    raise ModelPolicyError("no implemented adversarial Codex model satisfies diversity")


def resolve_profile(
    *,
    problem_id: str,
    statement_sha256: str,
    profile: str,
    root_adapter: str,
    root_provider: str,
    root_model: str,
    root_effort: str,
) -> dict[str, Any]:
    if profile == "custom":
        raise ModelPolicyError("custom policies must be supplied as canonical files")
    if profile not in PROFILES:
        raise ModelPolicyError("profile is unsupported")
    root = _root_actor(
        adapter=root_adapter,
        provider=root_provider,
        model=root_model,
        effort=root_effort,
    )
    if profile == "compatible":
        generator = _codex_actor(
            model="gpt-6-astra", effort="max", role="generator"
        )
        passes = [
            _codex_actor(
                model="gpt-6-astra", effort="xhigh", role="verifier", pass_index=1
            ),
            _codex_actor(
                model="gpt-6-astra", effort="xhigh", role="verifier", pass_index=2
            ),
        ]
        distinct_models = False
        adversarial_distinct = False
    elif profile == "balanced":
        generator = _codex_actor(
            model="gpt-6-astra", effort="max", role="generator"
        )
        passes = [
            _codex_actor(
                model="gpt-6-astra", effort="xhigh", role="verifier", pass_index=1
            ),
            _codex_actor(
                model=_secondary_codex_model(root_model),
                effort="max",
                role="verifier",
                pass_index=2,
            ),
        ]
        distinct_models = True
        adversarial_distinct = True
    elif profile == "economy":
        generator = _codex_actor(
            model="gpt-5.6-terra", effort="max", role="generator"
        )
        passes = [
            _codex_actor(
                model="gpt-6-astra", effort="xhigh", role="verifier", pass_index=1
            ),
            _codex_actor(
                model=_secondary_codex_model(root_model),
                effort="max",
                role="verifier",
                pass_index=2,
            ),
        ]
        distinct_models = True
        adversarial_distinct = True
    else:
        planned_model = "claude-opus-5"
        planned_provider = root_provider
        policy = {
            "schema_version": SCHEMA_VERSION,
            "problem_id": problem_id,
            "statement_sha256": statement_sha256,
            "revision": 1,
            "parent_policy_sha256": None,
            "profile": profile,
            "root": root,
            "generator": _codex_actor(
                model=MAX_DIVERSITY_CODEX_MODEL, effort="max", role="generator"
            ),
            "verifier": {
                "quorum": 2,
                "passes": [
                    _codex_actor(
                        model=MAX_DIVERSITY_CODEX_MODEL,
                        effort="max",
                        role="verifier",
                        pass_index=1,
                    ),
                    {
                        "pass_index": 2,
                        "role": "adversarial_full_claim_audit",
                        "adapter": "claude_cli",
                        "provider": planned_provider,
                        "model": planned_model,
                        "launch_model": "claude-opus-5[1m]",
                        "effort": "max",
                        "session_mode": "cold",
                    },
                ],
                "require_distinct_models": True,
                "require_distinct_providers": True,
                "require_adversarial_distinct_from_root": (
                    root_model != planned_model
                ),
                "automatic_tiebreaker": False,
            },
            "fallback_policy": "forbid",
            "selection_authority": "owner_wrapper",
        }
        return validate_policy(policy)
    policy = {
        "schema_version": SCHEMA_VERSION,
        "problem_id": problem_id,
        "statement_sha256": statement_sha256,
        "revision": 1,
        "parent_policy_sha256": None,
        "profile": profile,
        "root": root,
        "generator": generator,
        "verifier": {
            "quorum": 2,
            "passes": passes,
            "require_distinct_models": distinct_models,
            "require_distinct_providers": False,
            "require_adversarial_distinct_from_root": adversarial_distinct,
            "automatic_tiebreaker": False,
        },
        "fallback_policy": "forbid",
        "selection_authority": "owner_wrapper",
    }
    return validate_policy(policy)


def _strict_object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ModelPolicyError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ModelPolicyError(f"non-finite JSON constant is forbidden: {value}")


def read_policy_file(path: Path, expected_sha256: str) -> dict[str, Any]:
    if not path.is_absolute():
        raise ModelPolicyError("custom policy path must be absolute")
    _sha256(expected_sha256, label="expected policy digest")
    try:
        before = path.lstat()
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        raise ModelPolicyError("custom policy cannot be opened safely") from exc
    try:
        opened = os.fstat(descriptor)
        permitted_uids = {0, os.geteuid()} if hasattr(os, "geteuid") else {0}
        if (
            path.is_symlink()
            or not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or opened.st_uid not in permitted_uids
            or stat.S_IMODE(opened.st_mode) & 0o022
            or opened.st_size <= 0
            or opened.st_size > MAX_POLICY_BYTES
            or (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
            != (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        ):
            raise ModelPolicyError("custom policy file is unsafe")
        chunks: list[bytes] = []
        remaining = int(opened.st_size)
        while remaining:
            chunk = os.read(descriptor, min(65_536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        if remaining or os.read(descriptor, 1):
            raise ModelPolicyError("custom policy changed while reading")
        after_open = os.fstat(descriptor)
        after_path = path.lstat()
        identity = (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
        if (
            (after_open.st_dev, after_open.st_ino, after_open.st_size, after_open.st_mtime_ns)
            != identity
            or (after_path.st_dev, after_path.st_ino, after_path.st_size, after_path.st_mtime_ns)
            != identity
        ):
            raise ModelPolicyError("custom policy changed while reading")
        raw = b"".join(chunks)
    finally:
        os.close(descriptor)
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise ModelPolicyError("custom policy digest mismatch")
    try:
        value = json.loads(
            raw,
            object_pairs_hook=_strict_object_pairs,
            parse_constant=_reject_constant,
        )
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ModelPolicyError("custom policy is not strict UTF-8 JSON") from exc
    normalized = validate_policy(value)
    if canonical_bytes(normalized) != raw:
        raise ModelPolicyError("custom policy is not canonical JSON")
    return normalized


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    resolve = subparsers.add_parser("resolve", help="resolve one zero-model profile")
    resolve.add_argument("--problem-id", required=True)
    resolve.add_argument("--statement-sha256", required=True)
    resolve.add_argument("--profile", choices=sorted(PROFILES - {"custom"}), required=True)
    resolve.add_argument("--root-adapter", choices=sorted(ADAPTERS), required=True)
    resolve.add_argument("--root-provider", choices=sorted(PROVIDERS), required=True)
    resolve.add_argument("--root-model", required=True)
    resolve.add_argument("--root-effort", choices=sorted(EFFORTS), required=True)
    resolve.add_argument("--dry-run", action="store_true")

    validate = subparsers.add_parser("validate", help="validate one canonical policy file")
    validate.add_argument("--path", required=True)
    validate.add_argument("--sha256", required=True)

    subparsers.add_parser("capabilities", help="print the static capability registry")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "resolve":
            if not args.dry_run:
                raise ModelPolicyError(
                    "runtime integration is not implemented; resolve requires --dry-run"
                )
            policy = resolve_profile(
                problem_id=args.problem_id,
                statement_sha256=args.statement_sha256,
                profile=args.profile,
                root_adapter=args.root_adapter,
                root_provider=args.root_provider,
                root_model=args.root_model,
                root_effort=args.root_effort,
            )
            result = {
                "schema_version": "rethlas_model_policy_resolution_v1",
                "status": "resolved",
                "dry_run": True,
                "writes_performed": 0,
                "paid_actors_started": 0,
                "model_policy_sha256": policy_sha256(policy),
                "model_policy": policy,
            }
        elif args.command == "validate":
            policy = read_policy_file(Path(args.path), args.sha256)
            result = {
                "schema_version": "rethlas_model_policy_validation_v1",
                "status": "valid",
                "writes_performed": 0,
                "paid_actors_started": 0,
                "model_policy_sha256": policy_sha256(policy),
                "model_policy": policy,
            }
        else:
            result = {
                "schema_version": "rethlas_model_capability_registry_v1",
                "capabilities": [item.as_dict() for item in CAPABILITIES],
            }
    except (ModelPolicyError, OSError, ValueError, TypeError) as exc:
        print(f"AxiomRelay model policy rejected: {exc}", file=sys.stderr)
        return 70
    print(canonical_json(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
