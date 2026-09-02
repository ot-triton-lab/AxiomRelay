from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from agents import model_policy


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
POLICY_CLI = REPOSITORY_ROOT / "agents" / "model_policy.py"
POLICY_SCHEMA = REPOSITORY_ROOT / "agents" / "model_policy.schema.json"
STATEMENT_SHA256 = "a" * 64


def _resolve(profile: str) -> dict[str, object]:
    return model_policy.resolve_profile(
        problem_id="smoke/model-policy",
        statement_sha256=STATEMENT_SHA256,
        profile=profile,
        root_adapter="claude_cli",
        root_provider="vertex",
        root_model="claude-opus-5",
        root_effort="max",
    )


@pytest.fixture(scope="module")
def schema_validator() -> Draft202012Validator:
    schema = json.loads(POLICY_SCHEMA.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


@pytest.mark.parametrize(
    ("profile", "generator_model", "pass_models", "requires_distinct"),
    [
        (
            "compatible",
            "gpt-5.6-sol",
            ["gpt-5.6-sol", "gpt-5.6-sol"],
            False,
        ),
        (
            "balanced",
            "gpt-5.6-sol",
            ["gpt-5.6-sol", "gpt-5.6-terra"],
            True,
        ),
        (
            "economy",
            "gpt-5.6-terra",
            ["gpt-5.6-sol", "gpt-5.6-terra"],
            True,
        ),
    ],
)
def test_implemented_profiles_resolve_canonically(
    schema_validator: Draft202012Validator,
    profile: str,
    generator_model: str,
    pass_models: list[str],
    requires_distinct: bool,
) -> None:
    policy = _resolve(profile)
    schema_validator.validate(policy)

    assert policy["profile"] == profile
    assert policy["generator"]["model"] == generator_model
    assert [item["model"] for item in policy["verifier"]["passes"]] == pass_models
    assert policy["verifier"]["require_distinct_models"] is requires_distinct
    assert policy["generator"]["max_live_paid_lanes"] == 3
    assert policy["fallback_policy"] == "forbid"
    assert model_policy.validate_policy(policy) == policy
    assert model_policy.policy_sha256(policy) == hashlib.sha256(
        model_policy.canonical_bytes(policy)
    ).hexdigest()


def test_max_diversity_fails_closed_when_cold_claude_capability_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capabilities = tuple(
        replace(item, implemented=False)
        if "verifier" in item.roles and item.adapter == "claude_cli"
        else item
        for item in model_policy.CAPABILITIES
    )
    monkeypatch.setattr(model_policy, "CAPABILITIES", capabilities)
    with pytest.raises(
        model_policy.ModelPolicyError, match="designed but not implemented"
    ):
        _resolve("max_diversity")


def test_max_diversity_resolves_when_cold_claude_verifier_is_attested(
    schema_validator: Draft202012Validator,
) -> None:
    policy = _resolve("max_diversity")
    schema_validator.validate(policy)
    primary, adversarial = policy["verifier"]["passes"]
    assert primary["adapter"] == "codex_cli"
    assert primary["provider"] == "openai"
    assert primary["effort"] == "max"
    assert adversarial["adapter"] == "claude_cli"
    assert adversarial["provider"] == "vertex"
    assert adversarial["model"] == "claude-opus-5"
    assert adversarial["launch_model"] == "claude-opus-5[1m]"
    assert policy["verifier"]["require_distinct_models"] is True
    assert policy["verifier"]["require_distinct_providers"] is True
    assert policy["verifier"]["require_adversarial_distinct_from_root"] is False


def test_distinct_model_contract_rejects_same_model() -> None:
    policy = _resolve("balanced")
    policy["verifier"]["passes"][1]["model"] = "gpt-5.6-sol"
    with pytest.raises(model_policy.ModelPolicyError, match="distinct models"):
        model_policy.validate_policy(policy)


def test_planned_persistent_codex_root_is_not_exposed() -> None:
    with pytest.raises(
        model_policy.ModelPolicyError, match="designed but not implemented"
    ):
        model_policy.resolve_profile(
            problem_id="smoke/model-policy",
            statement_sha256=STATEMENT_SHA256,
            profile="balanced",
            root_adapter="codex_cli",
            root_provider="openai",
            root_model="gpt-5.6-sol",
            root_effort="max",
        )


def test_custom_policy_file_is_digest_bound_canonical_and_read_only(
    tmp_path: Path,
) -> None:
    policy = _resolve("balanced")
    policy["profile"] = "custom"
    raw = model_policy.canonical_bytes(policy)
    path = tmp_path / "policy.json"
    path.write_bytes(raw)
    path.chmod(0o600)
    digest = hashlib.sha256(raw).hexdigest()

    assert model_policy.read_policy_file(path.absolute(), digest) == policy
    with pytest.raises(model_policy.ModelPolicyError, match="digest mismatch"):
        model_policy.read_policy_file(path.absolute(), "b" * 64)

    noncanonical = tmp_path / "noncanonical.json"
    noncanonical.write_text(json.dumps(policy, indent=2), encoding="utf-8")
    noncanonical.chmod(0o600)
    with pytest.raises(model_policy.ModelPolicyError, match="not canonical"):
        model_policy.read_policy_file(
            noncanonical.absolute(),
            hashlib.sha256(noncanonical.read_bytes()).hexdigest(),
        )


@pytest.mark.parametrize(
    "profile", ["compatible", "balanced", "economy", "max_diversity"]
)
def test_cli_dry_run_starts_zero_paid_actors_and_writes_nothing(
    tmp_path: Path,
    profile: str,
) -> None:
    before = set(tmp_path.iterdir())
    environment = dict(os.environ)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    completed = subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            str(POLICY_CLI),
            "resolve",
            "--problem-id",
            "smoke/model-policy",
            "--statement-sha256",
            STATEMENT_SHA256,
            "--profile",
            profile,
            "--root-adapter",
            "claude_cli",
            "--root-provider",
            "vertex",
            "--root-model",
            "claude-opus-5",
            "--root-effort",
            "max",
            "--dry-run",
        ],
        cwd=tmp_path,
        env=environment,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    result = json.loads(completed.stdout)
    assert result["status"] == "resolved"
    assert result["dry_run"] is True
    assert result["writes_performed"] == 0
    assert result["paid_actors_started"] == 0
    assert result["model_policy"]["profile"] == profile
    assert set(tmp_path.iterdir()) == before


def test_cli_max_diversity_is_zero_paid_resolved(tmp_path: Path) -> None:
    environment = dict(os.environ)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    completed = subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            str(POLICY_CLI),
            "resolve",
            "--problem-id",
            "smoke/model-policy",
            "--statement-sha256",
            STATEMENT_SHA256,
            "--profile",
            "max_diversity",
            "--root-adapter",
            "claude_cli",
            "--root-provider",
            "vertex",
            "--root-model",
            "claude-opus-5",
            "--root-effort",
            "max",
            "--dry-run",
        ],
        cwd=tmp_path,
        env=environment,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    result = json.loads(completed.stdout)
    assert result["status"] == "resolved"
    assert result["model_policy"]["verifier"]["passes"][1] == {
        "pass_index": 2,
        "role": "adversarial_full_claim_audit",
        "adapter": "claude_cli",
        "provider": "vertex",
        "model": "claude-opus-5",
        "launch_model": "claude-opus-5[1m]",
        "effort": "max",
        "session_mode": "cold",
    }
    assert result["writes_performed"] == 0
    assert result["paid_actors_started"] == 0
    assert list(tmp_path.iterdir()) == []


def test_cli_refuses_non_dry_run_until_runtime_integration_exists(
    tmp_path: Path,
) -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            str(POLICY_CLI),
            "resolve",
            "--problem-id",
            "smoke/model-policy",
            "--statement-sha256",
            STATEMENT_SHA256,
            "--profile",
            "balanced",
            "--root-adapter",
            "claude_cli",
            "--root-provider",
            "vertex",
            "--root-model",
            "claude-opus-5",
            "--root-effort",
            "max",
        ],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert completed.returncode == 70
    assert "requires --dry-run" in completed.stderr
    assert list(tmp_path.iterdir()) == []
