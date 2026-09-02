from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
LAUNCHER = REPOSITORY_ROOT / "agents" / "verification" / "scripts" / "run_verifier.sh"
VERIFICATION_ROOT = REPOSITORY_ROOT / "agents" / "verification"


def _fake_claude(
    tmp_path: Path,
    *,
    provider: str = "vertex",
    auth_method: str = "third_party",
    subscription_type: str | None = None,
) -> tuple[Path, Path]:
    executable = tmp_path / "claude"
    calls = tmp_path / "calls.jsonl"
    executable.write_text(
        f"""#!{sys.executable}
import json
import os
import sys
path = os.environ.get("FAKE_CLAUDE_CALLS")
if path:
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(sys.argv[1:]) + "\\n")
if sys.argv[1:] == ["auth", "status"]:
    value = {{
        "loggedIn": True,
        "authMethod": {auth_method!r},
        "apiProvider": {provider!r},
    }}
    if {subscription_type!r} is not None:
        value["subscriptionType"] = {subscription_type!r}
    print(json.dumps(value))
    raise SystemExit(0)
raise SystemExit(90)
""",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    return executable, calls


def _environment(tmp_path: Path, profile: str) -> dict[str, str]:
    environment = dict(os.environ)
    environment.pop("AXIOM_RELAY_MODEL_POLICY_PROFILE", None)
    environment.pop("AXIOM_RELAY_VERIFIER_PRINT_COMMAND", None)
    environment.update(
        {
            "PYTHONDONTWRITEBYTECODE": "1",
            "RETHLAS_MODEL_POLICY_PROFILE": profile,
            "RETHLAS_VERIFIER_PRINT_CMD": "1",
            "HOME": str(tmp_path / "home"),
        }
    )
    (tmp_path / "home").mkdir(exist_ok=True)
    for name in (
        "CLAUDE_CODE_USE_VERTEX",
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_FOUNDRY",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_VERTEX_PROJECT_ID",
        "CLOUD_ML_REGION",
        "ANTHROPIC_DEFAULT_OPUS_MODEL",
        "VERIFY_CLAUDE_PROVIDER_MODEL",
        "VERIFY_REQUEST_TIMEOUT_SECONDS",
        "VERIFY_CLAUDE_TIMEOUT_SECONDS",
        "API_TIMEOUT_MS",
        "CLAUDE_STREAM_IDLE_TIMEOUT_MS",
        "CLAUDE_CODE_MAX_RETRIES",
        "CLAUDE_CODE_MAX_TURNS",
        "CLAUDE_CODE_MAX_OUTPUT_TOKENS",
        "VERIFY_MAX_OPERATIONAL_RESUMES",
        "VERIFY_CLAUDE_AUTH_MODE",
        "VERIFY_CLAUDE_AUTH_METHOD",
        "VERIFY_CLAUDE_SUBSCRIPTION_TYPE",
        "VERIFY_SERVER_HOST",
        "VERIFY_SERVER_PORT",
        "VERIFY_TLS_TERMINATED",
        "VERIFY_API_TOKEN",
    ):
        environment.pop(name, None)
    return environment


def test_axiom_relay_public_settings_select_verifier_profile(
    tmp_path: Path,
) -> None:
    environment = _environment(tmp_path, "compatible")
    environment.pop("RETHLAS_MODEL_POLICY_PROFILE")
    environment.pop("RETHLAS_VERIFIER_PRINT_CMD")
    environment["AXIOM_RELAY_MODEL_POLICY_PROFILE"] = "balanced"
    environment["AXIOM_RELAY_VERIFIER_PRINT_COMMAND"] = "1"

    completed = subprocess.run(
        [str(LAUNCHER)],
        cwd=VERIFICATION_ROOT,
        env=environment,
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "Verifier profile: balanced" in completed.stdout


@pytest.mark.parametrize(
    ("tls", "token", "diagnostic"),
    [
        ("0", bytes(range(32)).hex(), "requires VERIFY_TLS_TERMINATED=1"),
        ("1", "x", "requires a 256-bit random"),
    ],
)
def test_remote_verifier_configuration_fails_closed(
    tmp_path: Path, tls: str, token: str, diagnostic: str
) -> None:
    environment = _environment(tmp_path, "compatible")
    environment.update(
        {
            "VERIFY_SERVER_HOST": "0.0.0.0",
            "VERIFY_TLS_TERMINATED": tls,
            "VERIFY_API_TOKEN": token,
        }
    )

    completed = subprocess.run(
        [str(LAUNCHER)],
        cwd=VERIFICATION_ROOT,
        env=environment,
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )

    assert completed.returncode == 1
    assert diagnostic in completed.stderr


def test_remote_verifier_accepts_tls_and_random_256_bit_token(
    tmp_path: Path,
) -> None:
    environment = _environment(tmp_path, "compatible")
    environment.update(
        {
            "VERIFY_SERVER_HOST": "0.0.0.0",
            "VERIFY_TLS_TERMINATED": "1",
            "VERIFY_API_TOKEN": bytes(range(32)).hex(),
        }
    )

    completed = subprocess.run(
        [str(LAUNCHER)],
        cwd=VERIFICATION_ROOT,
        env=environment,
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "--host 0.0.0.0" in completed.stdout


@pytest.mark.parametrize(
    ("profile", "expected_primary", "expected_adversarial"),
    [
        ("compatible", "gpt-5.6-sol/xhigh", "gpt-5.6-sol/xhigh"),
        ("balanced", "gpt-5.6-sol/xhigh", "gpt-5.6-terra/max"),
        ("economy", "gpt-5.6-sol/xhigh", "gpt-5.6-terra/max"),
        (
            "max_diversity",
            "gpt-5.6-sol/max",
            "claude-opus-5[1m]/max via vertex",
        ),
    ],
)
def test_all_four_verifier_profiles_have_zero_model_print_smoke(
    tmp_path: Path,
    profile: str,
    expected_primary: str,
    expected_adversarial: str,
) -> None:
    environment = _environment(tmp_path, profile)
    fake_claude, calls = _fake_claude(tmp_path)
    environment["VERIFY_CLAUDE_BIN"] = str(fake_claude)
    environment["FAKE_CLAUDE_CALLS"] = str(calls)
    if profile == "max_diversity":
        environment.update(
            {
                "CLAUDE_CODE_USE_VERTEX": "1",
                "ANTHROPIC_VERTEX_PROJECT_ID": "test-project",
                "CLOUD_ML_REGION": "us-east5",
                "ANTHROPIC_DEFAULT_OPUS_MODEL": "vertex-opus-test@20260826",
            }
        )

    completed = subprocess.run(
        [str(LAUNCHER)],
        cwd=VERIFICATION_ROOT,
        env=environment,
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert f"Verifier profile: {profile}" in completed.stdout
    assert f"Primary verifier: {expected_primary}" in completed.stdout
    assert f"Adversarial verifier: {expected_adversarial}" in completed.stdout
    assert "Verifier request timeout: 86400s" in completed.stdout
    assert "Claude process/API timeout: 14400s/14000000ms" in completed.stdout
    assert (
        "Claude stream-idle timeout/internal retries/max turns: "
        "1800000ms/0/1"
        in completed.stdout
    )
    assert (
        "Claude requested output-token cap: 128000 "
        "(provider-attested effective cap is logged per execution)"
        in completed.stdout
    )
    assert (
        "Claude output mode: stream-json/raw-json "
        "(partial events enabled; local schema validation)"
        in completed.stdout
    )
    assert "Operational resume budget: 5" in completed.stdout
    assert "uvicorn" in completed.stdout
    if profile == "max_diversity":
        invocations = [json.loads(line) for line in calls.read_text().splitlines()]
        assert invocations == [["auth", "status"]]
        assert "Claude verifier executable SHA-256:" in completed.stdout
    else:
        assert not calls.exists()


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        (
            "CLAUDE_STREAM_IDLE_TIMEOUT_MS",
            "0",
            "CLAUDE_STREAM_IDLE_TIMEOUT_MS must be in [1, 1800000].",
        ),
        (
            "CLAUDE_STREAM_IDLE_TIMEOUT_MS",
            "1800001",
            "CLAUDE_STREAM_IDLE_TIMEOUT_MS must be in [1, 1800000].",
        ),
        (
            "CLAUDE_STREAM_IDLE_TIMEOUT_MS",
            "not-an-integer",
            "CLAUDE_STREAM_IDLE_TIMEOUT_MS must be in [1, 1800000].",
        ),
        (
            "CLAUDE_CODE_MAX_RETRIES",
            "-1",
            "CLAUDE_CODE_MAX_RETRIES must be a nonnegative integer.",
        ),
        (
            "CLAUDE_CODE_MAX_RETRIES",
            "not-an-integer",
            "CLAUDE_CODE_MAX_RETRIES must be a nonnegative integer.",
        ),
        (
            "CLAUDE_CODE_MAX_TURNS",
            "0",
            "CLAUDE_CODE_MAX_TURNS must be exactly 1.",
        ),
        (
            "CLAUDE_CODE_MAX_TURNS",
            "2",
            "CLAUDE_CODE_MAX_TURNS must be exactly 1.",
        ),
        (
            "CLAUDE_CODE_MAX_OUTPUT_TOKENS",
            "64000",
            "CLAUDE_CODE_MAX_OUTPUT_TOKENS must be exactly 128000.",
        ),
        (
            "CLAUDE_CODE_MAX_OUTPUT_TOKENS",
            "128001",
            "CLAUDE_CODE_MAX_OUTPUT_TOKENS must be exactly 128000.",
        ),
    ],
)
def test_claude_liveness_controls_fail_closed(
    tmp_path: Path, name: str, value: str, message: str
) -> None:
    environment = _environment(tmp_path, "compatible")
    environment[name] = value

    completed = subprocess.run(
        [str(LAUNCHER)],
        cwd=VERIFICATION_ROOT,
        env=environment,
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )

    assert completed.returncode == 1
    assert message in completed.stderr


def test_max_diversity_missing_opus_mapping_fails_before_service_start(
    tmp_path: Path,
) -> None:
    environment = _environment(tmp_path, "max_diversity")
    fake_claude, calls = _fake_claude(tmp_path)
    environment["VERIFY_CLAUDE_BIN"] = str(fake_claude)
    environment["FAKE_CLAUDE_CALLS"] = str(calls)

    completed = subprocess.run(
        [str(LAUNCHER)],
        cwd=VERIFICATION_ROOT,
        env=environment,
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )

    assert completed.returncode == 1
    assert "exact Vertex Opus model mapping" in completed.stderr
    invocations = [json.loads(line) for line in calls.read_text().splitlines()]
    assert invocations == [["auth", "status"]]
    assert "Uvicorn running" not in completed.stdout + completed.stderr


def test_max_diversity_accepts_subscription_without_vertex_configuration(
    tmp_path: Path,
) -> None:
    environment = _environment(tmp_path, "max_diversity")
    fake_claude, calls = _fake_claude(
        tmp_path,
        provider="anthropic",
        auth_method="claude.ai",
        subscription_type="max",
    )
    environment.update(
        {
            "VERIFY_CLAUDE_BIN": str(fake_claude),
            "FAKE_CLAUDE_CALLS": str(calls),
            "VERIFY_CLAUDE_AUTH_MODE": "subscription",
        }
    )

    completed = subprocess.run(
        [str(LAUNCHER)],
        cwd=VERIFICATION_ROOT,
        env=environment,
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "Adversarial verifier: claude-opus-5[1m]/max via anthropic" in (
        completed.stdout
    )
    assert [json.loads(line) for line in calls.read_text().splitlines()] == [
        ["auth", "status"]
    ]


def test_max_diversity_subscription_rejects_stored_api_auth(
    tmp_path: Path,
) -> None:
    environment = _environment(tmp_path, "max_diversity")
    fake_claude, calls = _fake_claude(
        tmp_path,
        provider="anthropic",
        auth_method="api_key",
    )
    environment.update(
        {
            "VERIFY_CLAUDE_BIN": str(fake_claude),
            "FAKE_CLAUDE_CALLS": str(calls),
            "VERIFY_CLAUDE_AUTH_MODE": "subscription",
        }
    )

    completed = subprocess.run(
        [str(LAUNCHER)],
        cwd=VERIFICATION_ROOT,
        env=environment,
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )

    assert completed.returncode == 1
    assert "requires a Claude subscription" in completed.stderr
    assert [json.loads(line) for line in calls.read_text().splitlines()] == [
        ["auth", "status"]
    ]


def test_max_diversity_explicit_opus_mapping_combines_with_vertex_settings(
    tmp_path: Path,
) -> None:
    environment = _environment(tmp_path, "max_diversity")
    fake_claude, calls = _fake_claude(tmp_path)
    environment["VERIFY_CLAUDE_BIN"] = str(fake_claude)
    environment["FAKE_CLAUDE_CALLS"] = str(calls)
    environment["VERIFY_CLAUDE_PROVIDER_MODEL"] = "vertex-opus-test@20260826"
    settings_dir = Path(environment["HOME"]) / ".claude"
    settings_dir.mkdir()
    settings = settings_dir / "settings.json"
    settings.write_text(
        json.dumps(
            {
                "env": {
                    "CLAUDE_CODE_USE_VERTEX": "1",
                    "ANTHROPIC_VERTEX_PROJECT_ID": "test-project",
                    "CLOUD_ML_REGION": "us-east5",
                }
            }
        ),
        encoding="utf-8",
    )
    settings.chmod(0o600)

    completed = subprocess.run(
        [str(LAUNCHER)],
        cwd=VERIFICATION_ROOT,
        env=environment,
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "Verifier profile: max_diversity" in completed.stdout
    assert "Primary verifier: gpt-5.6-sol/max" in completed.stdout
    assert "Adversarial verifier: claude-opus-5[1m]/max via vertex" in completed.stdout
    invocations = [json.loads(line) for line in calls.read_text().splitlines()]
    assert invocations == [["auth", "status"]]
