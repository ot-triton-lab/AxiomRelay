from __future__ import annotations

import ast
import hashlib
import json
import os
import py_compile
import selectors
import shutil
import sqlite3
import stat
import subprocess
import sys
import time
import tomllib
from pathlib import Path

import pytest

from agents.generation.guardian_launcher import LAUNCH_MANIFEST_SCHEMA_SHA256
from agents.hotjoin_adapter import GUARDIAN_CONTROL_SCHEMA_SHA256


RUNNER = Path(__file__).with_name("run_example.sh")
LEGACY_RUNNER = Path(__file__).with_name("run_legacy.sh")
CLAUDE_RUNNER = Path(__file__).with_name("run_claude_core.sh")
HOTJOIN_RUNNER = Path(__file__).with_name("run_hotjoin.sh")
GENERATION_ROOT = RUNNER.parents[1]
REQUIRED_MODULES = (
    "mcp",
    "requests",
    "numpy",
    "scipy",
    "sympy",
    "mpmath",
    "gmpy2",
)


def _linux_cohort_namespace_available() -> bool:
    """Return whether this host can exercise the production Linux capsule."""

    if not sys.platform.startswith("linux"):
        return True
    required = (Path("/usr/bin/unshare"), Path("/bin/true"))
    if any(not path.is_file() or not os.access(path, os.X_OK) for path in required):
        return False
    try:
        completed = subprocess.run(
            [
                "/usr/bin/unshare",
                "--user",
                "--map-root-user",
                "--mount",
                "--pid",
                "--fork",
                "--mount-proc",
                "/bin/true",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


_LINUX_COHORT_NAMESPACE_AVAILABLE = _linux_cohort_namespace_available()

TRUSTED_MCP_LOGICAL_MODULES = (
    "review.contracts",
    "review.critic",
    "mcp.publication_proof_context_v3",
    "mcp.proof_context",
    "mcp.advisor_client",
    "mcp.review_client",
    "mcp.verification_client",
    "mcp.server",
)
LEGACY_TRUSTED_MCP_LOGICAL_MODULES = (
    "mcp.publication_proof_context_v3",
    "mcp.proof_context",
    "mcp.legacy_verification_client",
    "mcp.legacy_server",
)


def _trusted_mcp_loader_source() -> str:
    runner_source = HOTJOIN_RUNNER.read_text(encoding="utf-8")
    opening = "TRUSTED_MCP_SECURE_LOADER=\"$(cat <<'PY'\n"
    start = runner_source.index(opening) + len(opening)
    end = runner_source.index("\nPY\n)\"", start)
    return runner_source[start:end]


def _trusted_mcp_snapshot(tmp_path: Path) -> tuple[Path, list[str]]:
    snapshot = tmp_path / "trusted-runtime"
    arguments: list[str] = []
    for logical_name in TRUSTED_MCP_LOGICAL_MODULES:
        relative = Path(*logical_name.split(".")).with_suffix(".py")
        source = (
            GENERATION_ROOT.parent / relative
            if logical_name.startswith("review.")
            else GENERATION_ROOT / relative
        )
        target = snapshot / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())
        target.chmod(0o400)
        arguments.extend(
            [
                logical_name,
                str(target),
                hashlib.sha256(target.read_bytes()).hexdigest(),
            ]
        )
    return snapshot, arguments


def _mcp_stdio_probe(
    command: list[str],
    *,
    cwd: Path,
    generation_root: Path,
    python_executable: Path,
    extra_env: dict[str, str] | None = None,
    tool_call: dict[str, object] | None = None,
) -> subprocess.CompletedProcess[str]:
    home = cwd / "home"
    home.mkdir(exist_ok=True)
    initialize = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "rethlas-zero-model-probe", "version": "1"},
        },
    }
    initialized = {
        "jsonrpc": "2.0",
        "method": "notifications/initialized",
        "params": {},
    }
    tools_list = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/list",
        "params": {},
    }
    process_environment = {
        "HOME": str(home),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": f"{python_executable.parent}:/usr/bin:/bin",
        "PYTHONDONTWRITEBYTECODE": "1",
        "RETHLAS_GENERATION_ROOT": str(generation_root),
    }
    process_environment.update(extra_env or {})
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=process_environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    assert process.stdin is not None
    assert process.stdout is not None
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    output_lines: list[str] = []
    try:
        process.stdin.write(json.dumps(initialize, separators=(",", ":")) + "\n")
        process.stdin.flush()
        if not selector.select(timeout=20):
            raise AssertionError("timed out waiting for MCP initialize response")
        first_line = process.stdout.readline()
        output_lines.append(first_line)
        if not first_line:
            process.stdin.close()
            process.stdin = None
            stdout_tail, stderr = process.communicate(timeout=20)
            output_lines.append(stdout_tail)
            return subprocess.CompletedProcess(
                command,
                process.returncode,
                "".join(output_lines),
                stderr,
            )

        requests = [initialized, tools_list]
        if tool_call is not None:
            requests.append(tool_call)
        for request in requests:
            process.stdin.write(json.dumps(request, separators=(",", ":")) + "\n")
        process.stdin.flush()
        for _request in requests[1:]:
            if not selector.select(timeout=20):
                raise AssertionError("timed out waiting for MCP response")
            output_lines.append(process.stdout.readline())

        process.stdin.close()
        process.stdin = None
        stdout_tail, stderr = process.communicate(timeout=20)
        output_lines.append(stdout_tail)
    except BaseException:
        process.kill()
        process.wait(timeout=5)
        raise
    finally:
        selector.close()
    return subprocess.CompletedProcess(
        command,
        process.returncode,
        "".join(output_lines),
        stderr,
    )


def _real_mcp_python() -> Path:
    configured = os.environ.get("RETHLAS_TEST_MCP_PYTHON")
    executable = (
        Path(configured).resolve(strict=True) if configured else Path(sys.executable)
    )
    probe = subprocess.run(
        [
            str(executable),
            "-I",
            "-B",
            "-c",
            (
                "try:\n"
                " from mcp.server.fastmcp import FastMCP\n"
                "except ImportError:\n"
                " from mcp.server.mcpserver import MCPServer as FastMCP\n"
                "import mcp.types\n"
                "assert callable(FastMCP)\n"
            ),
        ],
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )
    if probe.returncode != 0:
        if configured:
            pytest.fail(
                "RETHLAS_TEST_MCP_PYTHON lacks a compatible official MCP SDK: "
                + probe.stderr
            )
        pytest.skip("official MCP SDK unavailable; set RETHLAS_TEST_MCP_PYTHON")
    return executable


@pytest.mark.parametrize("entry", ["secure-loader", "direct-snapshot"])
def test_trusted_reasoning_mcp_completes_real_stdio_handshake(
    tmp_path: Path,
    entry: str,
) -> None:
    mcp_python = _real_mcp_python()
    snapshot, module_arguments = _trusted_mcp_snapshot(tmp_path)
    generation_root = tmp_path / "generation"
    generation_root.mkdir()
    command = (
        [
            str(mcp_python),
            "-I",
            "-B",
            "-c",
            _trusted_mcp_loader_source(),
            *module_arguments,
        ]
        if entry == "secure-loader"
        else [str(mcp_python), "-I", "-B", str(snapshot / "mcp" / "server.py")]
    )

    completed = _mcp_stdio_probe(
        command,
        cwd=generation_root,
        generation_root=generation_root,
        python_executable=mcp_python,
    )

    assert completed.returncode == 0, completed.stderr
    responses = [json.loads(line) for line in completed.stdout.splitlines()]
    assert [item["id"] for item in responses] == [1, 2]
    assert responses[0]["result"]["serverInfo"]["name"] == "reasoning-agent"
    tools = responses[1]["result"]["tools"]
    assert {item["name"] for item in tools} >= {
        "memory_search",
        "context_handoff_get",
        "route_review_status",
    }


def test_claude_root_mcp_completes_bound_tool_call_over_real_stdio(
    tmp_path: Path,
) -> None:
    runner, _fake_bin = _make_runner_tree(tmp_path)
    generation_root = runner.parents[1]
    mcp_python = _real_mcp_python()
    problem = generation_root / "data" / "example.md"
    statement_sha256 = hashlib.sha256(problem.read_bytes()).hexdigest()
    session_id = "12345678-1234-4123-8123-123456789abc"
    source_origin = generation_root.parent / "claude_core.py"
    source_snapshot = tmp_path / "bound-claude-core.py"
    source_snapshot.write_bytes(source_origin.read_bytes())
    source_snapshot.chmod(0o400)
    source_sha256 = hashlib.sha256(source_snapshot.read_bytes()).hexdigest()
    mcp_config = json.loads(
        (generation_root / ".mcp.json").read_text(encoding="utf-8")
    )["mcpServers"]["rethlas-root"]
    substitutions = {
        "${RETHLAS_CLAUDE_CORE_SNAPSHOT}": str(source_snapshot),
        "${RETHLAS_CLAUDE_CORE_SOURCE_SHA256}": source_sha256,
        "${RETHLAS_CLAUDE_CORE_ORIGIN}": str(source_origin),
    }
    bound_host_command = [
        str(mcp_python),
        *[substitutions.get(argument, argument) for argument in mcp_config["args"]],
    ]
    preparation_environment = dict(os.environ)
    preparation_environment.update(
        {
            "RETHLAS_CLAUDE_PINNED_PYTHON_SHA256": "3" * 64,
            "RETHLAS_CLAUDE_ROOT_LAUNCHER_SHA256": "4" * 64,
        }
    )
    preparation = subprocess.run(
        [
            *bound_host_command,
            "--prepare-root",
            "example",
            statement_sha256,
            session_id,
            "claude-opus-5",
            "claude-opus-5[1m]",
            "vertex",
            "2" * 64,
            "1" * 64,
            "test-claude-2.1.246",
            "1000000",
            "3" * 64,
            "4" * 64,
        ],
        cwd=generation_root,
        env=preparation_environment,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )
    assert preparation.returncode == 0, preparation.stdout + preparation.stderr
    tool_call = {
        "jsonrpc": "2.0",
        "id": 3,
        "method": "tools/call",
        "params": {
            "name": "memory_append_batch",
            "arguments": {
                "problem_id": "example",
                "items": [
                    {
                        "channel": "proof_steps",
                        "record": {"claim": "claude-root-mcp-checkpoint"},
                    }
                ],
            },
        },
    }
    completed = _mcp_stdio_probe(
        bound_host_command,
        cwd=generation_root,
        generation_root=generation_root,
        python_executable=mcp_python,
        extra_env={
            "RETHLAS_CLAUDE_ROOT_PROBLEM_ID": "example",
            "RETHLAS_CLAUDE_ROOT_STATEMENT_SHA256": statement_sha256,
            "RETHLAS_CLAUDE_ROOT_SESSION_ID": session_id,
            "RETHLAS_CLAUDE_ROOT_MODEL": "claude-opus-5",
            "RETHLAS_CLAUDE_ROOT_LAUNCH_MODEL": "claude-opus-5[1m]",
            "RETHLAS_CLAUDE_ROOT_PROVIDER": "vertex",
            "RETHLAS_CLAUDE_ROOT_PROVIDER_BINDING_SHA256": "2" * 64,
            "RETHLAS_CLAUDE_ROOT_CLI_SHA256": "1" * 64,
            "RETHLAS_CLAUDE_ROOT_CLI_VERSION": "test-claude-2.1.246",
            "RETHLAS_CLAUDE_ROOT_CONTEXT_WINDOW": "1000000",
            "RETHLAS_CLAUDE_ROOT_PYTHON_RUNTIME_SHA256": "3" * 64,
            "RETHLAS_CLAUDE_ROOT_LAUNCHER_SHA256": "4" * 64,
            "RETHLAS_CLAUDE_PINNED_PYTHON_SHA256": "3" * 64,
            "RETHLAS_CLAUDE_ROOT_CODEX_BIN": str(mcp_python),
        },
        tool_call=tool_call,
    )

    assert completed.returncode == 0, completed.stderr
    responses = [json.loads(line) for line in completed.stdout.splitlines()]
    assert [item["id"] for item in responses] == [1, 2, 3]
    tools = responses[1]["result"]["tools"]
    registered_tool_names = {item["name"] for item in tools}
    assert registered_tool_names == {
        "memory_search",
        "memory_append_batch",
        "search_matlas_theorems",
        "search_arxiv_theorems",
        "read_arxiv_primary",
        "prepare_pro_gap_query",
        "get_pro_gap_query",
        "ingest_pro_gap_response",
        "get_pro_gap_response",
        "run_three_route_cohort",
        "edit_blueprint",
        "write_blueprint",
        "verify_blueprint_service",
    }
    allowlist_line = next(
        line
        for line in CLAUDE_RUNNER.read_text(encoding="utf-8").splitlines()
        if line.startswith("CLAUDE_ALLOWED_TOOLS='")
    )
    launcher_allowlist = set(
        allowlist_line.removeprefix("CLAUDE_ALLOWED_TOOLS='")
        .removesuffix("'")
        .split(",")
    )
    assert launcher_allowlist - {"Read"} == {
        f"mcp__rethlas-root__{name}" for name in registered_tool_names
    }
    prepare_gap_schema = next(
        item for item in tools if item["name"] == "prepare_pro_gap_query"
    )["inputSchema"]
    assert "source_context_sha256" not in prepare_gap_schema["properties"]
    assert {
        "verified_fact_or_proof_ids",
        "failed_path_record_ids",
    } <= set(prepare_gap_schema["required"])
    get_gap_schema = next(
        item for item in tools if item["name"] == "get_pro_gap_query"
    )["inputSchema"]
    assert "expected_query_sha256" in get_gap_schema["required"]
    get_response_schema = next(
        item for item in tools if item["name"] == "get_pro_gap_response"
    )["inputSchema"]
    assert {
        "expected_query_sha256",
        "expected_response_sha256",
    } <= set(get_response_schema["required"])
    memory_tool = next(
        item for item in tools if item["name"] == "memory_append_batch"
    )
    memory_schema = json.dumps(memory_tool["inputSchema"], sort_keys=True)
    assert '"record"' in memory_schema
    assert '"additionalProperties": false' in memory_schema
    for channel in (
        "immediate_conclusions",
        "toy_examples",
        "counterexamples",
        "big_decisions",
        "subgoals",
        "proof_steps",
        "failed_paths",
        "verification_reports",
        "branch_states",
        "events",
    ):
        assert channel in memory_schema
    cohort_tool = next(
        item for item in tools if item["name"] == "run_three_route_cohort"
    )
    edit_tool = next(item for item in tools if item["name"] == "edit_blueprint")
    edit_schema = edit_tool["inputSchema"]
    assert set(edit_schema["properties"]) == {
        "problem_id",
        "statement_sha256",
        "base_blueprint_sha256",
        "old_string",
        "new_string",
        "replace_all",
    }
    assert "root_session_id" not in edit_schema["properties"]
    cohort_schema = json.dumps(cohort_tool["inputSchema"], sort_keys=True)
    for field in (
        "plan_id",
        "mechanism",
        "scope",
        "discriminating_test",
        "plan_summary",
        "subgoals",
        "motivation",
    ):
        assert f'"{field}"' in cohort_schema
    assert '"additionalProperties": false' in cohort_schema
    assert responses[2]["result"]["isError"] is False
    structured = responses[2]["result"]["structuredContent"]
    assert structured["problem_id"] == "example"
    assert structured["schema_version"] == (
        "rethlas_memory_batch_local_commit_receipt_v1"
    )


def test_trusted_reasoning_mcp_loader_rejects_changed_module_before_stdio(
    tmp_path: Path,
) -> None:
    snapshot, module_arguments = _trusted_mcp_snapshot(tmp_path)
    server_path = snapshot / "mcp" / "server.py"
    server_path.chmod(0o600)
    server_path.write_bytes(b"# changed after commitment\n")
    server_path.chmod(0o400)
    generation_root = tmp_path / "generation"
    generation_root.mkdir()

    completed = _mcp_stdio_probe(
        [
            sys.executable,
            "-I",
            "-B",
            "-c",
            _trusted_mcp_loader_source(),
            *module_arguments,
        ],
        cwd=generation_root,
        generation_root=generation_root,
        python_executable=Path(sys.executable),
    )

    assert completed.returncode == 70
    assert "module SHA-256 mismatch" in completed.stderr
    assert completed.stdout == ""


def test_trusted_reasoning_mcp_loader_rejects_preloaded_private_alias(
    tmp_path: Path,
) -> None:
    snapshot, module_arguments = _trusted_mcp_snapshot(tmp_path)
    generation_root = tmp_path / "generation"
    generation_root.mkdir()
    loader_source = (
        "import sys, types\n"
        "sys.modules['_rethlas_generation_mcp'] = "
        "types.ModuleType('_rethlas_generation_mcp')\n"
        + _trusted_mcp_loader_source()
    )

    completed = _mcp_stdio_probe(
        [
            sys.executable,
            "-I",
            "-B",
            "-c",
            loader_source,
            *module_arguments,
        ],
        cwd=generation_root,
        generation_root=generation_root,
        python_executable=Path(sys.executable),
    )

    assert completed.returncode == 70
    assert "trusted runtime package alias is already loaded" in completed.stderr
    assert completed.stdout == ""


_MOCK_GUARDIAN_LAUNCHER = r"""from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import re
import secrets
import subprocess
import sys


PRIVILEGED_TOKEN_ENV_NAMES = (
    "RETHLAS_REVIEW_CONTROL_TOKEN",
    "RETHLAS_GUARDIAN_CYCLE_TOKEN",
    "RETHLAS_RUNNER_CYCLE_TOKEN",
    "RETHLAS_STALE_RECOVERY_TOKEN",
)


def canonical(value: object) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def consume_token_fd(descriptor: int, *, label: str) -> str:
    assert descriptor >= 3
    raw = b""
    try:
        while len(raw) <= 64:
            chunk = os.read(descriptor, 65 - len(raw))
            if not chunk:
                break
            raw += chunk
    finally:
        os.close(descriptor)
    assert len(raw) == 64, f"{label} capability length mismatch"
    token = raw.decode("ascii")
    assert re.fullmatch(r"[0-9a-f]{64}", token) is not None
    return token


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser()
    value.add_argument("--owner-token-fd", type=int, required=True)
    value.add_argument("--db", type=pathlib.Path, required=True)
    value.add_argument("--adapter-path", type=pathlib.Path, required=True)
    value.add_argument("--adapter-sha256", required=True)
    value.add_argument("--guardian-path", type=pathlib.Path, required=True)
    value.add_argument("--runner-path", type=pathlib.Path, required=True)
    value.add_argument("--run-id", required=True)
    value.add_argument("--generation-control-instance-id", required=True)
    value.add_argument("--watchdog-id", required=True)
    value.add_argument(
        "--admission-mode",
        choices=("initial_new_cycle", "next_new_cycle", "same_cycle_resume"),
        required=True,
    )
    value.add_argument("--expected-cycle-id", required=True)
    value.add_argument("--expected-generation", type=int, required=True)
    value.add_argument("--expected-clock-sha256")
    value.add_argument("--capability-revision", type=int, required=True)
    value.add_argument("--policy-contract-sha256", required=True)
    value.add_argument("--policy-digest", required=True)
    value.add_argument(
        "--guardian-mode",
        choices=("hard_stop", "monitor_only"),
        default="hard_stop",
    )
    value.add_argument("--worker-cwd", type=pathlib.Path, required=True)
    value.add_argument("--problem-path", type=pathlib.Path, required=True)
    value.add_argument("--problem-relative-path", required=True)
    value.add_argument("--handoff-candidate-path", type=pathlib.Path)
    value.add_argument(
        "--worker-mode",
        choices=("runner_control", "opaque_guarded_command"),
        default="runner_control",
    )
    value.add_argument("worker_command", nargs=argparse.REMAINDER)
    return value


def main() -> int:
    arguments = sys.argv[1:]
    args = parser().parse_args(arguments)
    owner_token = consume_token_fd(args.owner_token_fd, label="owner")
    assert owner_token not in canonical(arguments)
    assert all(owner_token not in value for value in os.environ.values())
    assert all(name not in os.environ for name in PRIVILEGED_TOKEN_ENV_NAMES)
    assert args.worker_mode == "runner_control"
    assert args.expected_generation >= 1
    assert args.capability_revision >= 1
    assert re.fullmatch(r"cycle_[0-9a-f]{32}", args.expected_cycle_id) is not None
    assert re.fullmatch(r"[0-9a-f]{64}", args.policy_contract_sha256) is not None
    assert re.fullmatch(r"[0-9a-f]{64}", args.policy_digest) is not None
    if args.admission_mode == "same_cycle_resume":
        assert re.fullmatch(r"[0-9a-f]{64}", args.expected_clock_sha256 or "")
    else:
        assert args.expected_clock_sha256 is None
    if os.environ.get("MOCK_GUARDIAN_ENFORCE_CURRENT_CAPABILITY_REVISION"):
        state_path = pathlib.Path(os.environ["MOCK_CADENCE_STATE_FILE"])
        current_state = json.loads(state_path.read_text(encoding="utf-8"))
        assert args.capability_revision == current_state["capability_revision"]

    for source in (
        args.adapter_path,
        args.guardian_path,
        args.runner_path,
        args.problem_path,
    ):
        assert source.is_absolute() and source.is_file() and not source.is_symlink()
    assert hashlib.sha256(args.adapter_path.read_bytes()).hexdigest() == (
        args.adapter_sha256
    )
    worker = list(args.worker_command)
    if worker and worker[0] == "--":
        worker.pop(0)
    assert len(worker) >= 2
    assert pathlib.Path(worker[0]).is_absolute()
    assert not pathlib.Path(worker[0]).is_symlink()
    assert pathlib.Path(worker[1]).resolve() == args.adapter_path.resolve()
    assert "--runner-token-fd" not in worker
    assert "--control-token-fd" not in worker

    runner_token = secrets.token_hex(32)
    runner_read, runner_write = os.pipe()
    try:
        assert os.write(runner_write, runner_token.encode("ascii")) == 64
    finally:
        os.close(runner_write)
    child_environment = dict(os.environ)
    for name in PRIVILEGED_TOKEN_ENV_NAMES:
        child_environment.pop(name, None)

    calls_file = os.environ.get("MOCK_GUARDIAN_LAUNCHER_CALLS_FILE")
    if calls_file:
        record = {
            "admission_mode": args.admission_mode,
            "argv": arguments,
            "capability_revision": args.capability_revision,
            "expected_clock_sha256": args.expected_clock_sha256,
            "expected_cycle_id": args.expected_cycle_id,
            "expected_generation": args.expected_generation,
            "guardian_mode": args.guardian_mode,
            "owner_token_sha256": hashlib.sha256(
                owner_token.encode("ascii")
            ).hexdigest(),
            "capability_env_present": any(
                name in os.environ for name in PRIVILEGED_TOKEN_ENV_NAMES
            ),
            "runner_token_sha256": hashlib.sha256(
                runner_token.encode("ascii")
            ).hexdigest(),
            "worker_command": worker,
        }
        with pathlib.Path(calls_file).open("a", encoding="utf-8") as handle:
            handle.write(canonical(record) + "\n")

    if os.environ.get("MOCK_GUARDIAN_LAUNCHER_FAIL_BEFORE_DISPATCH"):
        print("mock guardian pre-dispatch failure", file=sys.stderr)
        return 70

    runtime_command = [worker[0], "-I", "-B", *worker[1:]]
    runtime_command.extend(("--runner-token-fd", str(runner_read)))
    try:
        completed = subprocess.run(
            runtime_command,
            cwd=args.worker_cwd,
            env=child_environment,
            pass_fds=(runner_read,),
            check=False,
        )
    finally:
        os.close(runner_read)
    result = {
        "report": {"direct_returncode": completed.returncode},
        "state": "completed" if completed.returncode == 0 else "failed",
    }
    print(canonical(result))
    return 0 if completed.returncode == 0 else 70


if __name__ == "__main__":
    raise SystemExit(main())
"""


def _site_packages(runtime_bin: Path) -> Path:
    return Path(
        subprocess.run(
            [
                str(runtime_bin / "python3"),
                "-I",
                "-B",
                "-c",
                "import sysconfig; print(sysconfig.get_paths()['purelib'])",
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    )


def _make_math_runtime(agents_dir: Path) -> Path:
    runtime = agents_dir / ".generation-venv"
    subprocess.run(
        [
            sys.executable,
            "-m",
            "venv",
            "--copies",
            "--without-pip",
            str(runtime),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    (runtime / ".lock").write_bytes(b"")
    runtime_bin = runtime / "bin"
    site_packages = _site_packages(runtime_bin)
    for module_name in REQUIRED_MODULES:
        package = site_packages / module_name
        package.mkdir()
        module_source = ""
        if module_name == "mcp":
            # Most runner tests exercise transport/control behavior without a
            # real MCP session.  This structural stub lets the trusted server
            # register its decorators; the dedicated stdio tests above use the
            # production official MCP SDK and would catch namespace shadowing.
            server_package = package / "server"
            server_package.mkdir()
            (server_package / "__init__.py").write_text("", encoding="utf-8")
            (server_package / "fastmcp.py").write_text("""class FastMCP:
    def __init__(self, name):
        self.name = name

    def tool(self, *, name):
        def register(function):
            return function
        return register

    def run(self):
        return None
""", encoding="utf-8")
            (package / "types.py").write_text("", encoding="utf-8")
        elif module_name == "requests":
            # The trusted verification client subclasses the public requests
            # base exception at import time; network calls remain outside this
            # runner-only mock suite.
            module_source = "class RequestException(Exception):\n    pass\n"
        (package / "__init__.py").write_text(module_source, encoding="utf-8")
    return runtime_bin


def _module_stub(fake_bin: Path, module_name: str) -> Path:
    return _site_packages(fake_bin) / module_name


def _make_runner_tree(tmp_path: Path) -> tuple[Path, Path]:
    generation = tmp_path / "agents" / "generation"
    tests_dir = generation / "tests"
    data_dir = generation / "data"
    tests_dir.mkdir(parents=True)
    data_dir.mkdir()
    for source in (
        RUNNER,
        LEGACY_RUNNER,
        CLAUDE_RUNNER,
        HOTJOIN_RUNNER,
    ):
        shutil.copy2(source, tests_dir / source.name)
    shutil.copy2(GENERATION_ROOT / "AGENTS.md", generation / "AGENTS.md")
    shutil.copy2(GENERATION_ROOT / "CLAUDE.md", generation / "CLAUDE.md")
    shutil.copy2(GENERATION_ROOT / ".mcp.json", generation / ".mcp.json")
    shutil.copy2(
        GENERATION_ROOT / "AGENTS.legacy.md",
        generation / "AGENTS.legacy.md",
    )
    shutil.copy2(GENERATION_ROOT / "guardian.py", generation / "guardian.py")
    (generation / "guardian_launcher.py").write_text(
        _MOCK_GUARDIAN_LAUNCHER,
        encoding="utf-8",
    )
    shutil.copy2(
        GENERATION_ROOT.parent / "advisor_bridge.py",
        generation.parent / "advisor_bridge.py",
    )
    shutil.copy2(
        GENERATION_ROOT.parent / "claude_core.py",
        generation.parent / "claude_core.py",
    )
    shutil.copy2(
        GENERATION_ROOT / "requirements-math-research.txt",
        generation / "requirements-math-research.txt",
    )
    shutil.copytree(GENERATION_ROOT / ".codex", generation / ".codex")
    shutil.copytree(GENERATION_ROOT / ".agents", generation / ".agents")
    shutil.copytree(
        GENERATION_ROOT / "mcp",
        generation / "mcp",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    shutil.copytree(
        GENERATION_ROOT.parent / "review",
        generation.parent / "review",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    (data_dir / "example.md").write_text("S", encoding="utf-8")

    fake_bin = _make_math_runtime(generation.parent)
    fake_codex = fake_bin / "codex"
    fake_codex.write_text(
        """#!/usr/bin/env python3
import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import sys
import time
import tomllib

if os.environ.get("MOCK_CODEX_DEFERRED_CHILD") == "1":
    time.sleep(float(os.environ["MOCK_CODEX_DEFERRED_FRONTIER_SECONDS"]))
    if os.environ.get("MOCK_CODEX_DEFERRED_ACTION") == "publication":
        publication_environment = dict(os.environ)
        publication_environment.pop("MOCK_CODEX_DEFERRED_CHILD")
        publication_environment.pop("MOCK_CODEX_DEFERRED_FRONTIER_SECONDS")
        publication_environment.pop("MOCK_CODEX_CALLS_FILE", None)
        completed = subprocess.run(
            [sys.executable, __file__, *sys.argv[1:]],
            env=publication_environment,
            stdin=subprocess.DEVNULL,
            check=False,
        )
        print("deferred collaboration publication complete", flush=True)
        raise SystemExit(completed.returncode)
    root = pathlib.Path.cwd()
    problem_id = os.environ["RETHLAS_EXPECTED_PROBLEM_ID"]
    draft = root / "results" / problem_id / "blueprint.md"
    draft.parent.mkdir(parents=True, exist_ok=True)
    draft.write_text("deferred collaboration frontier\\n", encoding="utf-8")
    print("deferred collaboration continuation complete", flush=True)
    raise SystemExit(0)

calls_file = os.environ.get("MOCK_CODEX_CALLS_FILE")
if calls_file:
    with pathlib.Path(calls_file).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(sys.argv) + "\\n")
isolation_probe = os.environ.get("MOCK_CODEX_ISOLATION_PROBE_FILE")
permission_configs = [
    value
    for index, value in enumerate(sys.argv)
    if index > 0 and sys.argv[index - 1] == "--config"
]
macos_cohort_profile = any(
    value == 'default_permissions="axiom-relay-cohort"'
    for value in permission_configs
)
if isolation_probe and "exec" in sys.argv and macos_cohort_profile:
    pathlib.Path(isolation_probe).write_text(
        json.dumps(
            {
                "current_problem": True,
                "current_plan": True,
                "current_candidate_projection": True,
                "current_memory": True,
                "current_results": True,
                "other_problem": False,
                "other_plan": False,
                "other_statement_candidate_projection": False,
                "other_problem_candidate_projection": False,
                "other_memory": False,
                "other_results": False,
                "old_log": False,
                "host_claude_state": False,
                "git_history": False,
                "codex_auth_write_succeeded": True,
                "codex_history": False,
                "codex_models_cache": False,
                "claude_history": False,
                "root_home_is_private_tmpfs": False,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    isolation_probe = None
if isolation_probe and "exec" in sys.argv:
    root = pathlib.Path.cwd()
    expected_plan = pathlib.Path(os.environ["MOCK_CODEX_EXPECTED_PLAN"])
    expected_candidate = pathlib.Path(
        os.environ["MOCK_CODEX_EXPECTED_CANDIDATE"]
    )
    other_statement_candidate = pathlib.Path(
        os.environ["MOCK_CODEX_OTHER_STATEMENT_CANDIDATE"]
    )
    other_problem_candidate = pathlib.Path(
        os.environ["MOCK_CODEX_OTHER_PROBLEM_CANDIDATE"]
    )
    codex_auth = pathlib.Path.home() / ".codex" / "auth.json"
    codex_auth_write_succeeded = False
    if codex_auth.is_file():
        try:
            codex_auth.write_text(
                codex_auth.read_text(encoding="utf-8") + "private runtime write",
                encoding="utf-8",
            )
            codex_auth_write_succeeded = True
        except OSError:
            pass
    root_home_is_private_tmpfs = False
    for mount_line in pathlib.Path("/proc/self/mountinfo").read_text().splitlines():
        fields = mount_line.split()
        separator = fields.index("-")
        if (
            fields[4] == "/root"
            and fields[separator + 1] == "tmpfs"
            and fields[separator + 2] == "rethlas-empty-root-home"
        ):
            root_home_is_private_tmpfs = True
            break
    probe = {
        "current_problem": (root / "data" / "example.md").is_file(),
        "current_plan": (root / expected_plan).is_file(),
        "current_candidate_projection": (
            root / expected_candidate
        ).is_file(),
        "current_memory": (root / "memory" / "example" / "current.txt").is_file(),
        "current_results": (root / "results" / "example" / "current.txt").is_file(),
        "other_problem": (root / "data" / "other.md").exists(),
        "other_plan": (root / ".claude_core_inputs" / "other").exists(),
        "other_statement_candidate_projection": (
            root / other_statement_candidate
        ).exists(),
        "other_problem_candidate_projection": (
            root / other_problem_candidate
        ).exists(),
        "other_memory": (root / "memory" / "other").exists(),
        "other_results": (root / "results" / "other").exists(),
        "old_log": (root / "logs" / "other").exists(),
        "host_claude_state": (root.parent / ".claude_core" / "other").exists(),
        "git_history": (root.parents[1] / ".git" / "sentinel").exists(),
        "codex_auth_write_succeeded": codex_auth_write_succeeded,
        "codex_history": (pathlib.Path.home() / ".codex" / "history.jsonl").exists(),
        "codex_models_cache": (
            pathlib.Path.home() / ".codex" / "models_cache.json"
        ).exists(),
        "claude_history": (pathlib.Path.home() / ".claude" / "projects").exists(),
        "root_home_is_private_tmpfs": root_home_is_private_tmpfs,
    }
    pathlib.Path(isolation_probe).write_text(
        json.dumps(probe, sort_keys=True), encoding="utf-8"
    )
if "--version" in sys.argv:
    print("codex-mock 1.0")
    raise SystemExit(0)
if sys.argv[1:] == ["login", "status"]:
    if os.environ.get("MOCK_CODEX_LOGGED_IN", "1") == "1":
        print("Logged in using ChatGPT")
        raise SystemExit(0)
    print("Not logged in")
    raise SystemExit(1)
if sys.argv[1:2] == ["sandbox"]:
    separator = sys.argv.index("--")
    probe_arguments = sys.argv[separator + 1 :]
    assert probe_arguments[:2] == ["/bin/sh", "-c"]
    assert len(probe_arguments) >= 6
    pathlib.Path(probe_arguments[-1]).write_text("ready", encoding="utf-8")
    raise SystemExit(0)
if "exec" in sys.argv and os.environ.get("MOCK_CODEX_DEFERRED_FRONTIER_SECONDS"):
    deferred_environment = dict(os.environ)
    deferred_environment["MOCK_CODEX_DEFERRED_CHILD"] = "1"
    subprocess.Popen(
        [sys.executable, __file__, *sys.argv[1:]],
        env=deferred_environment,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    print("foreground Codex turn returned before its collaboration continuation")
    raise SystemExit(0)
assert "--dangerously-bypass-approvals-and-sandbox" not in sys.argv
if macos_cohort_profile:
    assert "--sandbox" not in sys.argv
    assert 'permissions.axiom-relay-cohort.network.enabled=false' in permission_configs
else:
    assert sys.argv[sys.argv.index("--sandbox") + 1] == "workspace-write"
if os.environ.get("RETHLAS_RUN_MODE") in {"core", "legacy"}:
    assert "--strict-config" in sys.argv
    assert "--ignore-user-config" in sys.argv
    assert "--ignore-rules" in sys.argv
if os.environ.get("RETHLAS_RUNTIME_PROFILE") == "legacy":
    assert os.environ.get("BASH_ENV") == "/dev/null"
    login_shell_configs = [
        value
        for index, value in enumerate(sys.argv)
        if index > 0
        and sys.argv[index - 1] == "--config"
        and value.startswith("allow_login_shell=")
    ]
    assert login_shell_configs == ["allow_login_shell=false"]
shell_policy_configs = [
    value
    for index, value in enumerate(sys.argv)
    if index > 0
    and sys.argv[index - 1] == "--config"
    and value.startswith("shell_environment_policy=")
]
assert len(shell_policy_configs) == 1
shell_policy = tomllib.loads(
    "value=" + shell_policy_configs[0].split("=", 1)[1]
)["value"]
assert shell_policy == {
    "inherit": "none",
    "set": {
        "PATH": (
            f"{pathlib.Path(sys.executable).parent.resolve()}"
            ":/usr/bin:/bin:/usr/sbin:/sbin"
        ),
        "BASH_ENV": "/dev/null",
    },
}
safe_path = shell_policy["set"]["PATH"]
assert pathlib.Path(shutil.which("python", path=safe_path)).resolve() == (
    pathlib.Path(sys.executable).parent / "python"
).resolve()
assert pathlib.Path(shutil.which("python3", path=safe_path)).resolve() == (
    pathlib.Path(sys.executable).parent / "python3"
).resolve()
(pathlib.Path.cwd() / "shell_environment_policy_seen.json").write_text(
    json.dumps(shell_policy), encoding="utf-8"
)
reasoning_mcp_configs = [
    value
    for index, value in enumerate(sys.argv)
    if index > 0
    and sys.argv[index - 1] == "--config"
    and value.startswith("mcp_servers.reasoning_")
]
assert len(reasoning_mcp_configs) == 3
reasoning_mcp_servers = {
    raw.split("=", 1)[0].removeprefix("mcp_servers."): tomllib.loads(
        "value=" + raw.split("=", 1)[1]
    )["value"]
    for raw in reasoning_mcp_configs
}
assert set(reasoning_mcp_servers) == {
    "reasoning_agent",
    "reasoning_checkpoint_primary",
    "reasoning_checkpoint_recovery",
}
reasoning_mcp = reasoning_mcp_servers["reasoning_agent"]
if os.environ.get("MOCK_EXPECT_NO_ADVISOR_ENV"):
    for name in (
        "RETHLAS_ADVISOR_RECEIPTS_ROOT",
        "RETHLAS_EXPECTED_HOTJOIN_RUN_ID",
    ):
        assert name not in os.environ
        assert all(name not in server["env"] for server in reasoning_mcp_servers.values())
assert set(reasoning_mcp) == {
    "command",
    "args",
    "cwd",
    "env",
    "required",
    "tool_timeout_sec",
    "default_tools_approval_mode",
    "disabled_tools",
}
for checkpoint_id in (
    "reasoning_checkpoint_primary",
    "reasoning_checkpoint_recovery",
):
    assert set(reasoning_mcp_servers[checkpoint_id]) == {
        "command",
        "args",
        "cwd",
        "env",
        "required",
        "tool_timeout_sec",
        "default_tools_approval_mode",
        "enabled_tools",
    }
common_keys = {
    "command",
    "args",
    "cwd",
    "env",
    "required",
    "default_tools_approval_mode",
}
assert len(
    {
        json.dumps(
            {key: server[key] for key in common_keys},
            sort_keys=True,
            separators=(",", ":"),
        )
        for server in reasoning_mcp_servers.values()
    }
) == 1
assert reasoning_mcp["disabled_tools"] == ["memory_append_batch"]
for checkpoint_id in (
    "reasoning_checkpoint_primary",
    "reasoning_checkpoint_recovery",
):
    checkpoint = reasoning_mcp_servers[checkpoint_id]
    assert checkpoint["enabled_tools"] == ["memory_append_batch"]
    assert checkpoint["tool_timeout_sec"] == 60
    assert checkpoint["required"] is True
    assert checkpoint["default_tools_approval_mode"] == "approve"
assert pathlib.Path(reasoning_mcp["command"]).is_absolute()
expected_generation_python = os.environ.get(
    "MOCK_EXPECTED_GENERATION_PYTHON",
    sys.executable,
)
assert pathlib.Path(reasoning_mcp["command"]).resolve() == pathlib.Path(
    expected_generation_python
).resolve()
loader_args = reasoning_mcp["args"]
assert loader_args[:3] == ["-I", "-B", "-c"]
assert "trusted MCP secure-loader failed" in loader_args[3]
module_arguments = loader_args[4:]
legacy_profile = reasoning_mcp["env"].get("RETHLAS_RUNTIME_PROFILE") == "legacy"
expected_modules = (
    [
        "mcp.publication_proof_context_v3",
        "mcp.proof_context",
        "mcp.legacy_verification_client",
        "mcp.legacy_server",
    ]
    if legacy_profile
    else [
        "review.contracts",
        "review.critic",
        "mcp.publication_proof_context_v3",
        "mcp.proof_context",
        "mcp.advisor_client",
        "mcp.review_client",
        "mcp.verification_client",
        "mcp.server",
    ]
)
assert len(module_arguments) == 3 * len(expected_modules)
trusted_mcp_modules = {
    module_arguments[index]: pathlib.Path(module_arguments[index + 1])
    for index in range(0, len(module_arguments), 3)
}
assert list(trusted_mcp_modules) == expected_modules
server_logical_name = "mcp.legacy_server" if legacy_profile else "mcp.server"
if legacy_profile:
    config_values = [
        sys.argv[index + 1]
        for index, value in enumerate(sys.argv[:-1])
        if value == "--config"
    ]
    root_model = sys.argv[sys.argv.index("-m") + 1]
    effort_config = next(
        value
        for value in config_values
        if value.startswith("model_reasoning_effort=")
    )
    root_effort = tomllib.loads(
        "value=" + effort_config.split("=", 1)[1]
    )["value"]
    assert (
        "agents.default_subagent_model=" + json.dumps(root_model)
    ) in config_values
    assert (
        "agents.default_subagent_reasoning_effort=" + json.dumps(root_effort)
    ) in config_values
    assert config_values.count("project_doc_max_bytes=0") == 1
    instruction_configs = [
        value
        for value in config_values
        if value.startswith("developer_instructions=")
    ]
    assert len(instruction_configs) == 1
    legacy_instructions = tomllib.loads(
        "value=" + instruction_configs[0].split("=", 1)[1]
    )["value"]
    assert len(legacy_instructions.encode("utf-8")) <= 32_768
    assert "# Legacy Math Reasoning Profile" in legacy_instructions
    assert "$legacy-three-route" in legacy_instructions
    assert "## Continuous supervisor" not in legacy_instructions
    assert "T+60" not in legacy_instructions
    (pathlib.Path.cwd() / "legacy_instructions_seen.json").write_text(
        json.dumps(
            {
                "bytes": len(legacy_instructions.encode("utf-8")),
                "sha256": hashlib.sha256(
                    legacy_instructions.encode("utf-8")
                ).hexdigest(),
            }
        ),
        encoding="utf-8",
    )
    assert "LEGACY_GENERATION_CONTROL_TOKEN" not in os.environ
    assert "RETHLAS_GENERATION_CONTROL_TOKEN" not in os.environ
    assert "RETHLAS_LEGACY_ALLOW_OFFLINE_DRAFT" not in os.environ
    assert not {
        "RETHLAS_EXPECTED_HOTJOIN_RUN_ID",
        "RETHLAS_ADVISOR_RECEIPTS_ROOT",
        "RETHLAS_REVIEW_CADENCE_POLICY",
        "RETHLAS_CONTEXT_GUARD_POLICY",
        "RETHLAS_REVIEW_ADAPTER_PATH",
        "RETHLAS_REVIEW_ADAPTER_SHA256",
        "RETHLAS_REVIEW_DB",
        "RETHLAS_RESOLVED_COST_POLICY_JSON",
        "RETHLAS_RESOLVED_COST_POLICY_SHA256",
    } & set(reasoning_mcp["env"])
for index in range(0, len(module_arguments), 3):
    module_path = pathlib.Path(module_arguments[index + 1])
    module_sha256 = module_arguments[index + 2]
    assert module_path.is_absolute() and module_path.is_file()
    assert hashlib.sha256(module_path.read_bytes()).hexdigest() == module_sha256
assert pathlib.Path(reasoning_mcp["cwd"]).resolve() == pathlib.Path.cwd().resolve()
assert reasoning_mcp["tool_timeout_sec"] == 3600
assert reasoning_mcp["required"] is True
# Every trusted MCP role is noninteractive; approval_policy=never cannot cancel
# a call while waiting for an unavailable prompt.
assert reasoning_mcp["default_tools_approval_mode"] == "approve"
assert "NumPy, SciPy, SymPy, mpmath, and gmpy2" in sys.argv[-1]
(pathlib.Path.cwd() / "reasoning_mcp_config_seen.json").write_text(
    json.dumps(reasoning_mcp), encoding="utf-8"
)
(pathlib.Path.cwd() / "reasoning_mcp_server_map_seen.json").write_text(
    json.dumps(reasoning_mcp_servers), encoding="utf-8"
)
if os.environ.get("MOCK_EXPECT_VERIFY_PROOF_URL"):
    assert os.environ["VERIFY_PROOF_URL"] == os.environ["MOCK_EXPECT_VERIFY_PROOF_URL"]
if os.environ.get("MOCK_EXPECT_VERIFY_API_TOKEN"):
    assert os.environ["VERIFY_API_TOKEN"] == os.environ["MOCK_EXPECT_VERIFY_API_TOKEN"]
root = pathlib.Path.cwd()
problem_id = os.environ["RETHLAS_EXPECTED_PROBLEM_ID"]
if os.environ.get("MOCK_FRONTIER_PROGRESS"):
    counter_path = root / ".mock_frontier_counter"
    counter = (
        int(counter_path.read_text(encoding="utf-8")) + 1
        if counter_path.exists()
        else 1
    )
    counter_path.write_text(str(counter), encoding="utf-8")
    progress_limit = int(os.environ.get("MOCK_FRONTIER_PROGRESS_LIMIT", "0"))
    if progress_limit == 0 or counter <= progress_limit:
        draft = root / "results" / problem_id / "blueprint.md"
        draft.parent.mkdir(parents=True, exist_ok=True)
        draft.write_text(f"mock trusted frontier {counter}\\n", encoding="utf-8")
generation_control_state = os.environ.get("MOCK_GENERATION_CONTROL_STATE")
if generation_control_state:
    assert generation_control_state in {
        "waiting_cost_gate",
        "waiting_owner_advisor_decision",
    }
    snapshot_mcp = trusted_mcp_modules[server_logical_name].resolve().parent
    sys.path.insert(0, str(snapshot_mcp))
    if legacy_profile:
        # The isolated Legacy server cannot construct a wait state and the
        # Codex process receives no generation-control token. Corrupt the sole
        # owner-created running record to prove the owner CLI rejects it.
        control_root = root.parent / ".generation_control"
        control_paths = list(control_root.glob("*.json"))
        assert len(control_paths) == 1
        control_path = control_paths[0]
        control = json.loads(control_path.read_text(encoding="utf-8"))
        control["state"] = generation_control_state
        control["reason"] = "mock forbidden legacy wait"
        control["evidence_record_ids"] = ["mock_forbidden_wait"]
        control_path.write_text(
            json.dumps(
                control,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\\n",
            encoding="utf-8",
        )
    else:
        import server as trusted_generation_server

        if generation_control_state == "waiting_cost_gate":
            cost_manifest = json.loads(
                os.environ["RETHLAS_RESOLVED_COST_POLICY_JSON"]
            )
            event_payload = {
                "event_type": "recursive_proving_round",
                "status": generation_control_state,
                "orchestration_cost": {
                    "cost_gate_policy": cost_manifest["policy"],
                    "cost_gate_policy_manifest_sha256": os.environ[
                        "RETHLAS_RESOLVED_COST_POLICY_SHA256"
                    ],
                },
            }
        else:
            event_payload = {
                "event_type": "advisor_checkpoint",
                "status": generation_control_state,
                "owner_action_required": True,
                "browser_dispatch_authorized": False,
                "advisor_request_id": None,
            }
        event_receipt = trusted_generation_server.memory_append(
            problem_id, "events", event_payload
        )
        branch_receipt = trusted_generation_server.branch_update(
            problem_id,
            "mock-control-branch",
            {"status": generation_control_state},
        )
        trusted_generation_server._set_generation_control(
            problem_id,
            instance_id=os.environ["RETHLAS_GENERATION_CONTROL_TOKEN"],
            state=generation_control_state,
            reason="mock evidence-backed unfinished yield",
            evidence_record_ids=[
                event_receipt["record_id"],
                branch_receipt["record_id"],
            ],
        )
verified = root / "results" / problem_id / "blueprint_verified.md"
verified.parent.mkdir(parents=True, exist_ok=True)
proof = b"mock verified proof"
verified.write_bytes(proof)
if os.environ.get("MOCK_PUBLICATION") == "trusted":
    sys.path.insert(0, str(root / "mcp"))
    from proof_context import (
        aggregate_adaptive_context_digest,
        aggregate_context_digest,
        build_item_context,
        extract_verification_target,
        parse_blueprint,
    )
    manifest = parse_blueprint(proof.decode("utf-8"), target_statement="S")
    attestations = []
    for item_id in manifest.item_ids:
        context = build_item_context(manifest, item_id, max_chars=200000)
        attestations.append({
            "item_id": item_id,
            "disposition": "verified",
            "final_round": 0,
            "expanded_proof_ids": [],
            "max_chars": 200000,
            "context_digest": context["digest"],
            "verdict": "correct",
        })
    receipt = pathlib.Path(os.environ["RETHLAS_RECEIPTS_ROOT"]) / f"{problem_id}.json"
    receipt.parent.mkdir(parents=True, exist_ok=True)
    receipt.write_text(json.dumps({
        "schema_version": "rethlas-publication-v6",
        "state": "active",
        "problem_id": problem_id,
        "statement_source_digest": os.environ["RETHLAS_EXPECTED_STATEMENT_SHA256"],
        "canonical_target_digest": hashlib.sha256(
            extract_verification_target("S").encode("utf-8")
        ).hexdigest(),
        "proof_digest": hashlib.sha256(proof).hexdigest(),
        "context_digest": aggregate_context_digest(manifest),
        "adaptive_context_digest": aggregate_adaptive_context_digest(
            manifest, attestations
        ),
        "item_context_attestations": attestations,
        "checked_item_ids": list(manifest.item_ids),
        "verified_path": str(verified.absolute()),
        "published_bytes": len(proof),
        "published_at_utc": "2026-08-26T12:00:00+00:00",
        "verification_quorum": 2,
        "verification_passes": [
            {
                "pass_index": 1,
                "verification_attempt_id": "veratt_" + "1" * 32,
                "verifier_run_id": "mock-verifier-run-1",
                "verifier_model": "gpt-5.6-sol",
                "verifier_reasoning_effort": "max",
                "verifier_service_version": "0.3.0",
                "verification_role": "primary",
                "response_sha256": "1" * 64,
                "verdict": "correct",
            },
            {
                "pass_index": 2,
                "verification_attempt_id": "veratt_" + "2" * 32,
                "verifier_run_id": "mock-verifier-run-2",
                "verifier_model": "gpt-5.6-sol",
                "verifier_reasoning_effort": "max",
                "verifier_service_version": "0.3.0",
                "verification_role": "adversarial_full_claim_audit",
                "response_sha256": "2" * 64,
                "verdict": "correct",
            },
        ],
        "supersedes": [],
        "proof_context": {
            "schema_version": "rethlas_publication_proof_context_v3",
            "source_sha256": hashlib.sha256(
                trusted_mcp_modules[
                    "mcp.publication_proof_context_v3"
                ].read_bytes()
            ).hexdigest(),
            "proof_item_schema_version": 1,
            "proof_context_schema_version": 2,
            "aggregate_context_schema_version": 1,
            "adaptive_aggregate_context_schema_version": 2,
        },
        "verification_limits": {
            "context_max_chars": 200000,
            "max_expansion_rounds": 2,
            "max_expanded_proofs": 8,
            "max_expanded_proof_chars": 200000,
            "max_proof_items": 20000,
            "max_receipt_bytes": 16000000,
            "max_blueprint_bytes": 8000000,
            "max_blueprint_chars": 2000000,
        },
        "publication_target_precondition": {
            "kind": "absent",
            "st_dev": None,
            "st_ino": None,
            "st_size": None,
            "st_mtime_ns": None,
            "content_sha256": None,
        },
    }), encoding="utf-8")
elif os.environ.get("MOCK_PUBLICATION") == "trusted_v2":
    sys.path.insert(0, str(root / "mcp"))
    from proof_context import (
        aggregate_adaptive_context_digest,
        aggregate_context_digest,
        build_item_context,
        parse_blueprint,
    )
    manifest = parse_blueprint(proof.decode("utf-8"), target_statement="S")
    attestations = []
    for item_id in manifest.item_ids:
        context = build_item_context(manifest, item_id, max_chars=200000)
        attestations.append({
            "item_id": item_id,
            "disposition": "verified",
            "final_round": 0,
            "expanded_proof_ids": [],
            "max_chars": 200000,
            "context_digest": context["digest"],
            "verdict": "correct",
        })
    receipt = pathlib.Path(os.environ["RETHLAS_RECEIPTS_ROOT"]) / f"{problem_id}.json"
    receipt.parent.mkdir(parents=True, exist_ok=True)
    receipt.write_text(json.dumps({
        "schema_version": "rethlas-publication-v2",
        "problem_id": problem_id,
        "statement_digest": os.environ["RETHLAS_EXPECTED_STATEMENT_SHA256"],
        "proof_digest": hashlib.sha256(proof).hexdigest(),
        "context_digest": aggregate_context_digest(manifest),
        "adaptive_context_digest": aggregate_adaptive_context_digest(
            manifest, attestations
        ),
        "item_context_attestations": attestations,
        "checked_item_ids": list(manifest.item_ids),
        "verified_path": str(verified.absolute()),
        "published_bytes": len(proof),
    }), encoding="utf-8")
elif os.environ.get("MOCK_PUBLICATION") == "tamper":
    source_server = root / "mcp" / pathlib.Path(
        trusted_mcp_modules[server_logical_name]
    ).name
    source_server.write_text("# tampered publisher\\n", encoding="utf-8")
elif os.environ.get("MOCK_PUBLICATION") == "transient_tamper":
    source_server = root / "mcp" / pathlib.Path(
        trusted_mcp_modules[server_logical_name]
    ).name
    original = source_server.read_bytes()
    source_server.write_text("# transient malicious publisher\\n", encoding="utf-8")
    try:
        snapshot_server = trusted_mcp_modules[server_logical_name].resolve()
        assert not snapshot_server.is_relative_to(root.resolve())
        assert snapshot_server.read_bytes() == original
        (root / "snapshot_restart_checked").write_text(
            str(snapshot_server), encoding="utf-8"
        )
    finally:
        source_server.write_bytes(original)
elif os.environ.get("MOCK_PUBLICATION") == "snapshot_restart_tamper":
    snapshot_server = trusted_mcp_modules[server_logical_name].resolve()
    original = snapshot_server.read_bytes()
    original_mode = snapshot_server.stat().st_mode
    executed_marker = root / "snapshot_restart_payload_executed"
    checked_marker = root / "snapshot_restart_loader_checked"
    malicious = (
        "from pathlib import Path\\n"
        f"Path({str(executed_marker)!r}).write_text('executed', encoding='utf-8')\\n"
    ).encode("utf-8") + original
    restart_environment = {
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        **reasoning_mcp["env"],
    }
    control_paths = list((root.parent / ".generation_control").glob("*.json"))
    assert len(control_paths) == 1
    restart_environment["RETHLAS_GENERATION_CONTROL_TOKEN"] = (
        control_paths[0].name.split("_", 1)[0]
    )
    try:
        snapshot_server.chmod(original_mode | 0o200)
        snapshot_server.write_bytes(malicious)
        snapshot_server.chmod(original_mode & ~0o222)
        rejected = subprocess.run(
            [
                reasoning_mcp["command"],
                *reasoning_mcp["args"],
                "--",
                "--generation-control-state",
                problem_id,
            ],
            cwd=reasoning_mcp["cwd"],
            env=restart_environment,
            capture_output=True,
            text=True,
            check=False,
        )
        assert rejected.returncode == 70
        assert "module SHA-256 mismatch" in rejected.stderr
        assert not executed_marker.exists()
    finally:
        snapshot_server.chmod(original_mode | 0o200)
        snapshot_server.write_bytes(original)
        snapshot_server.chmod(original_mode)
    accepted = subprocess.run(
        [
            reasoning_mcp["command"],
            *reasoning_mcp["args"],
            "--",
            "--generation-control-state",
            problem_id,
        ],
        cwd=reasoning_mcp["cwd"],
        env=restart_environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert accepted.returncode == 0, accepted.stderr
    assert accepted.stdout.strip() == "running"
    assert not executed_marker.exists()
    checked_marker.write_text(str(snapshot_server), encoding="utf-8")
mock_token_usage = os.environ.get("MOCK_CODEX_TOKEN_USAGE")
if mock_token_usage:
    assert mock_token_usage.isdigit() and int(mock_token_usage) > 0
    print("tokens used")
    print(f"{int(mock_token_usage):,}")
""",
        encoding="utf-8",
    )
    fake_codex.chmod(0o755)
    fake_claude = fake_bin / "claude"
    fake_claude.write_text(
        f"""#!{sys.executable}
import json
import os
import sys

if "--version" in sys.argv:
    print("2.1.246 (Claude Code mock)")
    raise SystemExit(0)
if sys.argv[1:] in (
    ["auth", "status"],
    ["--setting-sources", "project", "auth", "status"],
):
    logged_in = os.environ.get("MOCK_CLAUDE_LOGGED_IN", "1") == "1"
    if os.environ.get("CLAUDE_CODE_USE_VERTEX") in {{"1", "true", "TRUE"}}:
        provider = "vertex"
    elif os.environ.get("CLAUDE_CODE_USE_BEDROCK") in {{"1", "true", "TRUE"}}:
        provider = "bedrock"
    elif os.environ.get("CLAUDE_CODE_USE_FOUNDRY") in {{"1", "true", "TRUE"}}:
        provider = "foundry"
    else:
        provider = "anthropic"
    provider = os.environ.get("MOCK_CLAUDE_API_PROVIDER", provider)
    auth_method = os.environ.get("MOCK_CLAUDE_AUTH_METHOD", "third_party")
    subscription_type = os.environ.get("MOCK_CLAUDE_SUBSCRIPTION_TYPE")
    status = {{
        "loggedIn": logged_in,
        "authMethod": auth_method,
        "apiProvider": provider,
    }}
    if subscription_type is not None:
        status["subscriptionType"] = subscription_type
    print(json.dumps(status))
    raise SystemExit(0 if logged_in else 1)
calls_file = os.environ.get("MOCK_CLAUDE_CALLS_FILE")
if calls_file:
    with open(calls_file, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(sys.argv[1:]) + "\\n")
environment_file = os.environ.get("MOCK_CLAUDE_ENV_FILE")
if environment_file:
    with open(environment_file, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({{
            "CLAUDE_CODE_EXTRA_BODY": os.environ.get("CLAUDE_CODE_EXTRA_BODY"),
            "CLAUDE_CODE_MAX_OUTPUT_TOKENS": os.environ.get("CLAUDE_CODE_MAX_OUTPUT_TOKENS"),
        }}, sort_keys=True) + "\\n")
if "--print" in sys.argv:
    thinking_events = int(os.environ.get("MOCK_CLAUDE_THINKING_EVENTS", "0"))
    for index in range(thinking_events):
        print(json.dumps({{
            "type": "system",
            "subtype": "thinking_tokens",
            "estimated_tokens": index + 1,
            "estimated_tokens_delta": 1,
        }}))
    thinking_marker = os.environ.get("MOCK_CLAUDE_THINKING_MARKER")
    if thinking_marker:
        print(json.dumps({{
            "type": "assistant",
            "message": {{
                "role": "assistant",
                "content": [{{"type": "thinking", "thinking": thinking_marker}}],
            }},
        }}))
    max_output_failures = int(os.environ.get("MOCK_CLAUDE_MAX_OUTPUT_FAILURES", "0"))
    max_output_state = os.environ.get("MOCK_CLAUDE_MAX_OUTPUT_STATE")
    max_output_attempt = 0
    if max_output_state:
        try:
            with open(max_output_state, encoding="utf-8") as handle:
                max_output_attempt = int(handle.read() or "0")
        except FileNotFoundError:
            pass
        max_output_attempt += 1
        with open(max_output_state, "w", encoding="utf-8") as handle:
            handle.write(str(max_output_attempt))
    if max_output_failures > 0 and max_output_attempt <= max_output_failures:
        max_output_tokens = os.environ.get("CLAUDE_CODE_MAX_OUTPUT_TOKENS", "")
        markerless = os.environ.get("MOCK_CLAUDE_MAX_OUTPUT_MARKERLESS") == "1"
        message = {{
            "type": "message",
            "role": "assistant",
            "content": [{{"type": "text", "text": "output exhausted"}}],
        }}
        if not markerless:
            message["is_api_error_message"] = True
            message["api_error"] = "max_output_tokens"
        assistant_event = {{
            "type": "assistant",
            "message": message,
        }}
        if markerless:
            assistant_event["error"] = "max_output_tokens"
        print(json.dumps(assistant_event))
        result_text = (
            "API Error: Claude's response exceeded the "
            + max_output_tokens
            + " output token maximum."
        )
        if markerless:
            result_text += " To configure this behavior, adjust the output cap."
        print(json.dumps({{
            "type": "result",
            "subtype": "success",
            "is_error": True,
            "terminal_reason": "api_error",
            "result": result_text,
        }}))
        raise SystemExit(1)
    if os.environ.get("MOCK_CLAUDE_RECOVERED_MAX_OUTPUT") == "1":
        print(json.dumps({{
            "type": "assistant",
            "message": {{
                "type": "message",
                "role": "assistant",
                "content": [{{"type": "text", "text": "transient exhaustion"}}],
                "is_api_error_message": True,
                "api_error": "max_output_tokens",
            }},
        }}))
    if os.environ.get("MOCK_CLAUDE_GENERIC_ERROR") == "1":
        print(json.dumps({{
            "type": "result",
            "subtype": "success",
            "is_error": True,
            "terminal_reason": "api_error",
            "result": "API Error: unknown provider failure",
        }}))
        raise SystemExit(1)
    print(json.dumps({{"type": "result", "subtype": "success", "is_error": False, "result": "mock Claude turn complete"}}))
    raise SystemExit(0)
raise SystemExit(0)
""",
        encoding="utf-8",
    )
    fake_claude.chmod(0o755)
    return tests_dir / "run_example.sh", fake_bin


def _mock_environment(
    runner: Path,
    fake_bin: Path,
    *,
    mode: str,
    problem_file: str = "data/example.md",
    extra_environment: dict[str, str] | None = None,
) -> dict[str, str]:
    runner_tmp = runner.parent.parent / ".runner-tmp"
    runner_tmp.mkdir(exist_ok=True)
    isolated_home = runner_tmp / "home"
    isolated_home.mkdir(exist_ok=True)
    environment = dict(os.environ)
    # Host test runners must not silently select an AxiomRelay mode for a child
    # fixture. Individual cases add these values explicitly when required.
    for name in (
        "AXIOM_RELAY_RUN_MODE",
        "AXIOM_RELAY_MAIN_AGENT",
        "AXIOM_RELAY_MODEL_POLICY_PROFILE",
        "AXIOM_RELAY_REVIEW_RUN_ID",
        "AXIOM_RELAY_CLAUDE_BIN",
        "AXIOM_RELAY_CODEX_BIN",
        "AXIOM_RELAY_PRINT_COMMAND",
        "AXIOM_RELAY_CLAUDE_SESSION_ID",
        "AXIOM_RELAY_CLAUDE_TAKEOVER_FROM",
        "AXIOM_RELAY_CLAUDE_OWNER_PROMPT",
        "AXIOM_RELAY_CLAUDE_CONTEXT_WINDOW",
        "AXIOM_RELAY_CLAUDE_AUTH_MODE",
    ):
        environment.pop(name, None)
    environment.pop("RETHLAS_RUN_MODE", None)
    environment.pop("RETHLAS_HOTJOIN_RUN_ID", None)
    environment.pop("RETHLAS_MAIN_AGENT", None)
    environment.pop("RETHLAS_MODEL_POLICY_PROFILE", None)
    environment.pop("RETHLAS_CLAUDE_BIN", None)
    environment.pop("RETHLAS_CLAUDE_CODEX_BIN", None)
    environment.pop("RETHLAS_CLAUDE_ROOT_PRINT_CMD", None)
    environment.pop("RETHLAS_CLAUDE_ROOT_SESSION_ID", None)
    environment.pop("RETHLAS_CLAUDE_ROOT_TAKEOVER_FROM", None)
    environment.pop("RETHLAS_CLAUDE_ROOT_CANARY", None)
    environment.pop("RETHLAS_CLAUDE_ROOT_PROBLEM_ID", None)
    environment.pop("RETHLAS_CLAUDE_ROOT_STATEMENT_SHA256", None)
    environment.pop("RETHLAS_CLAUDE_ROOT_MODEL", None)
    environment.pop("RETHLAS_CLAUDE_ROOT_ORCHESTRATION_MODE", None)
    environment.pop("RETHLAS_CLAUDE_ROOT_PYTHON_RUNTIME_SHA256", None)
    environment.pop("RETHLAS_CLAUDE_ROOT_LAUNCHER_SHA256", None)
    environment.pop("RETHLAS_CLAUDE_ROOT_CODEX_BIN", None)
    environment.pop("RETHLAS_COHORT_CODEX_BIN", None)
    environment.pop("RETHLAS_COHORT_CODEX_FD", None)
    environment.pop("RETHLAS_COHORT_CODEX_SHA256", None)
    environment.pop("RETHLAS_COHORT_HOST_SOURCE_FD", None)
    environment.pop("RETHLAS_COHORT_HOST_SOURCE_ORIGIN", None)
    environment.pop("RETHLAS_COHORT_HOST_SOURCE_SHA256", None)
    environment.pop("RETHLAS_COHORT_HOST_SOURCE_SNAPSHOT", None)
    environment.pop("CLAUDE_CODE_MAX_OUTPUT_TOKENS", None)
    environment.pop("CLAUDE_CODE_EXTRA_BODY", None)
    environment.pop("CLAUDE_CONFIG_DIR", None)
    environment.pop("CLAUDE_CODE_USE_VERTEX", None)
    environment.pop("CLAUDE_CODE_USE_BEDROCK", None)
    environment.pop("CLAUDE_CODE_USE_FOUNDRY", None)
    environment.pop("ANTHROPIC_API_KEY", None)
    environment.pop("ANTHROPIC_AUTH_TOKEN", None)
    environment.pop("ANTHROPIC_BASE_URL", None)
    environment.pop("ANTHROPIC_VERTEX_PROJECT_ID", None)
    environment.pop("CLOUD_ML_REGION", None)
    environment.pop("ANTHROPIC_DEFAULT_OPUS_MODEL", None)
    environment.pop("ANTHROPIC_DEFAULT_FABLE_MODEL", None)
    environment.update(
        {
            "PATH": f"{fake_bin}{os.pathsep}{environment['PATH']}",
            "HOME": str(isolated_home),
            "TMPDIR": str(runner_tmp),
            "MAX_ITERATIONS": "1",
            "TIMER_INTERVAL_SECONDS": "1",
            "LOG_DIR": str(runner.parents[3] / "logs"),
            "VERIFY_HEALTH_URL": "http://127.0.0.1:1/health",
            "RETHLAS_LEGACY_ALLOW_OFFLINE_DRAFT": "1",
            "MOCK_PUBLICATION": mode,
            "PROBLEM_FILE": problem_file,
        }
    )
    environment.update(extra_environment or {})
    environment["MOCK_EXPECTED_GENERATION_PYTHON"] = environment.get(
        "RETHLAS_GENERATION_PYTHON_BIN",
        str(runner.parents[2] / ".generation-venv" / "bin" / "python"),
    )
    return environment


def _run_mock(
    tmp_path: Path,
    *,
    mode: str,
    problem_file: str = "data/example.md",
    extra_environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    runner, fake_bin = _make_runner_tree(tmp_path)
    environment = _mock_environment(
        runner,
        fake_bin,
        mode=mode,
        problem_file=problem_file,
        extra_environment=extra_environment,
    )
    return subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )


def _install_mock_official_native_claude(
    home: Path,
    executable_source: Path,
    *,
    version: str = "2.1.258",
) -> tuple[Path, Path]:
    versioned = home / ".local" / "share" / "claude" / "versions" / version
    app_binary = (
        home
        / ".local"
        / "share"
        / "claude"
        / "ClaudeCode.app"
        / "Contents"
        / "MacOS"
        / "claude"
    )
    current = home / ".local" / "bin" / "claude"
    versioned.parent.mkdir(parents=True)
    app_binary.parent.mkdir(parents=True)
    current.parent.mkdir(parents=True)
    shutil.copy2(executable_source, versioned)
    os.link(versioned, app_binary)
    current.symlink_to(versioned)
    return current, versioned


def test_claude_runtime_separates_lifetime_and_snapshot_locks(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    environment = _mock_environment(
        runner,
        fake_bin,
        mode="trusted",
        extra_environment={
            "RETHLAS_MAIN_AGENT": "opus",
            "RETHLAS_CLAUDE_ROOT_PRINT_CMD": "1",
        },
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    runtime_root = runner.parents[2] / ".generation-venv"
    lifetime_lock = runtime_root / ".lock"
    snapshot_lock = runtime_root / ".snapshot.lock"
    assert lifetime_lock.is_file() and not lifetime_lock.is_symlink()
    assert snapshot_lock.is_file() and not snapshot_lock.is_symlink()
    assert (lifetime_lock.stat().st_dev, lifetime_lock.stat().st_ino) != (
        snapshot_lock.stat().st_dev,
        snapshot_lock.stat().st_ino,
    )


def _install_mock_cadence_adapter(tmp_path: Path) -> tuple[Path, Path, Path]:
    adapter_path = tmp_path / "agents" / "hotjoin_adapter.py"
    state_path = tmp_path / "cadence-state.json"
    calls_path = tmp_path / "cadence-calls.jsonl"
    adapter_source = r"""from __future__ import annotations

import hashlib
import json
import os
import pathlib
import sys
import tomllib


COST_POLICY_MANIFEST = json.loads(os.environ["RETHLAS_RESOLVED_COST_POLICY_JSON"])
assert set(COST_POLICY_MANIFEST) == {"schema_version", "policy", "authority"}
assert COST_POLICY_MANIFEST["schema_version"] == "rethlas_resolved_cost_policy_v1"
assert COST_POLICY_MANIFEST["authority"] == "owner_wrapper"
COST_POLICY_MANIFEST_SHA256 = hashlib.sha256(
    json.dumps(
        COST_POLICY_MANIFEST,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
).hexdigest()
assert COST_POLICY_MANIFEST_SHA256 == os.environ[
    "RETHLAS_RESOLVED_COST_POLICY_SHA256"
]


REVIEW = {
    "policy_id": "rethlas_route_review_150m_v2",
    "clock": "earliest_durable_wall_and_same_boot_monotonic",
    "approved_guardian_launcher_sha256": "__APPROVED_GUARDIAN_LAUNCHER_SHA256__",
    "approved_guardian_sha256": "__APPROVED_GUARDIAN_SHA256__",
    "approved_guardian_runner_sha256": "__APPROVED_GUARDIAN_RUNNER_SHA256__",
    "guardian_control_schema_sha256": "__GUARDIAN_CONTROL_SCHEMA_SHA256__",
    "guardian_launch_manifest_schema_sha256": (
        "__GUARDIAN_LAUNCH_MANIFEST_SCHEMA_SHA256__"
    ),
    "cycle_seconds": 9000,
    "review_1_due_seconds": 3600,
    "review_1_deadline_seconds": 4200,
    "review_2_due_seconds": 7200,
    "review_2_deadline_seconds": 7800,
    "review_boundary_mode": "cooperative_drain_then_deadline_interrupt",
    "review_drain_grace_seconds": 300,
    "review_execution_grace_seconds": 300,
    "review_partial_report_max_bytes": 16384,
    "close_notice_due_seconds": 8820,
    "hard_stop_due_seconds": 9000,
    "review_verdicts": ["green", "yellow", "red"],
    "two_yellow_without_progress_is_red": True,
    "review_is_independent": True,
    "review_is_not_fact_check": True,
    "hard_stop_interrupt_is_expected": True,
    "guardian_enforcement_ready": True,
    "max_concurrent_proof_lanes": 3,
    "owner_cost_gate_policy": COST_POLICY_MANIFEST["policy"],
    "owner_cost_gate_enabled": COST_POLICY_MANIFEST["policy"] == "owner_gated",
    "owner_cost_gate_policy_manifest_schema": COST_POLICY_MANIFEST["schema_version"],
    "owner_cost_gate_policy_manifest_sha256": COST_POLICY_MANIFEST_SHA256,
}
CONTINUOUS = {
    "policy_id": "rethlas_continuous_supervisor_v1",
    "clock": "global_rolling_wall_and_same_boot_monotonic",
    "review_interval_seconds": 3600,
    "summary_grace_seconds": 300,
    "review_execution_grace_seconds": 300,
    "renewal_interval_seconds": 9000,
    "max_concurrent_proof_lanes": 3,
    "root_review_interrupt": False,
    "root_renewal_interrupt": False,
    "summary_terminal_state": "parked",
    "reviewer_process_authority": False,
    "review_clock_resets_on_renewal": False,
    "renewal_is_terminal": False,
    "owner_wait_suspends_clocks": True,
    "parked_child_resume_scope": "same_root_thread_epoch",
    "root_physical_turn_auto_continuation": False,
    "cause_bound_root_successors": True,
    "review_host_progress_without_root": True,
    "owner_messages_deferred_during_host_review": True,
    "next_cohort_requires_round_finish_or_review_restart": True,
    "review_history_required": True,
    "transition_reducer_schema": "rethlas_continuous_transition_reducer_v1",
    "admission_decision_schema": "rethlas_continuous_admission_decision_v1",
    "paid_root_dispositions": [
        "continue_next_cycle",
        "continuous_intent_successor_required",
        "continuous_verdict_successor_required",
        "initial_start_allowed",
    ],
    "verified_completion_requires_owner_receipt_validation": True,
    "state_machine_schema": "rethlas_continuous_state_machine_v1",
    "state_machine_sha256": "f" * 64,
    "resource_policy_sha256": "e" * 64,
    "guardian_mode": "monitor_only",
    "guardian_enforcement_ready": True,
    "owner_cost_gate_policy": COST_POLICY_MANIFEST["policy"],
    "owner_cost_gate_enabled": COST_POLICY_MANIFEST["policy"] == "owner_gated",
    "owner_cost_gate_policy_manifest_schema": COST_POLICY_MANIFEST["schema_version"],
    "owner_cost_gate_policy_manifest_sha256": COST_POLICY_MANIFEST_SHA256,
    "approved_guardian_launcher_sha256": "__APPROVED_GUARDIAN_LAUNCHER_SHA256__",
    "approved_guardian_sha256": "__APPROVED_GUARDIAN_SHA256__",
    "approved_guardian_runner_sha256": "__APPROVED_GUARDIAN_RUNNER_SHA256__",
    "guardian_control_schema_sha256": "__GUARDIAN_CONTROL_SCHEMA_SHA256__",
    "guardian_launch_manifest_schema_sha256": (
        "__GUARDIAN_LAUNCH_MANIFEST_SCHEMA_SHA256__"
    ),
}
CONTEXT = {
    "policy_id": "rethlas_context_guard_v1",
    "occupancy_numerator": "last.inputTokens",
    "occupancy_denominator": "modelContextWindow",
    "cached_input_tokens_reduce_occupancy": False,
    "observe": {"ratio_gte": 0.60, "headroom_lte": 112000},
    "checkpoint_required": {"ratio_gte": 0.65, "headroom_lte": 96000},
    "fresh_thread_required": {"ratio_gte": 0.70, "headroom_lte": 80000},
    "emergency": {"ratio_gte": 0.82, "headroom_lte": 48000},
    "compaction_forces_fresh_thread": True,
    "max_handoff_utf8_bytes": 32768,
    "fresh_thread_must_not_resume_or_fork": True,
}
def canonical(value: object) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def review_driver_commitment(driver: pathlib.Path) -> dict[str, str]:
    assert driver.name == "server_driver.py"
    mcp_root = driver.parent
    review_root = mcp_root.parent / "review"
    logical_paths = (
        "generation/mcp/__init__.py",
        "generation/mcp/advisor_client.py",
        "generation/mcp/publication_proof_context_v3.py",
        "generation/mcp/proof_context.py",
        "generation/mcp/review_client.py",
        "generation/mcp/server.py",
        "generation/mcp/server_driver.py",
        "generation/mcp/verification_client.py",
        "review/__init__.py",
        "review/contracts.py",
        "review/critic.py",
    )
    entries = []
    driver_sha256 = ""
    for logical_path in logical_paths:
        relative = pathlib.Path(logical_path)
        source = (
            mcp_root / relative.name
            if relative.parts[:2] == ("generation", "mcp")
            else review_root / relative.name
        )
        raw = source.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        entries.append({"path": logical_path, "sha256": digest, "size": len(raw)})
        if logical_path == "generation/mcp/server_driver.py":
            driver_sha256 = digest
    manifest = {
        "schema_version": "rethlas_review_driver_package_v1",
        "files": sorted(entries, key=lambda item: item["path"]),
    }
    return {
        "driver_sha256": driver_sha256,
        "package_sha256": hashlib.sha256(canonical(manifest).encode()).hexdigest(),
    }


review_digest = hashlib.sha256(canonical(REVIEW).encode()).hexdigest()
continuous_digest = hashlib.sha256(canonical(CONTINUOUS).encode()).hexdigest()
context_digest = hashlib.sha256(canonical(CONTEXT).encode()).hexdigest()
contract_material = {
    "schema_version": "rethlas-policy-contract-v1",
    "review_cadence_policy": {**REVIEW, "policy_sha256": review_digest},
    "continuous_supervisor_policy": {
        **CONTINUOUS,
        "policy_sha256": continuous_digest,
    },
    "context_guard_policy": {**CONTEXT, "policy_sha256": context_digest},
}
contract = {
    **contract_material,
    "contract_sha256": hashlib.sha256(
        canonical(contract_material).encode()
    ).hexdigest(),
}

arguments = sys.argv[1:]
commands = {
    "policy-contract",
    "init",
    "status",
    "cadence-control-state",
    "control-capability-bind",
    "stale-recovery-capability-prepare",
    "cadence-admit",
    "cadence-close",
    "stale-turn-reconcile",
    "review-drive",
    "guarded-review-drive",
    "context-handoff-prepare",
    "review-status",
    "run-generator",
}
command = next(value for value in arguments if value in commands)

PRIVILEGED_TOKEN_ENV_NAMES = (
    "RETHLAS_REVIEW_CONTROL_TOKEN",
    "RETHLAS_GUARDIAN_CYCLE_TOKEN",
    "RETHLAS_RUNNER_CYCLE_TOKEN",
    "RETHLAS_STALE_RECOVERY_TOKEN",
)
OWNER_CONTROL_COMMANDS = {
    "control-capability-bind",
    "cadence-admit",
    "cadence-close",
    "review-drive",
}


def read_capability_fd(option: str) -> str | None:
    if option not in arguments:
        return None
    assert arguments.count(option) == 1
    descriptor_text = arguments[arguments.index(option) + 1]
    assert descriptor_text.isdecimal()
    descriptor = int(descriptor_text)
    assert descriptor >= 3
    raw = b""
    try:
        while len(raw) <= 64:
            chunk = os.read(descriptor, 65 - len(raw))
            if not chunk:
                break
            raw += chunk
    finally:
        os.close(descriptor)
    assert len(raw) == 64
    token = raw.decode("ascii")
    assert all(character in "0123456789abcdef" for character in token)
    assert token not in canonical(arguments)
    assert all(token not in value for value in os.environ.values())
    return token


control_token = read_capability_fd("--control-token-fd")
runner_token = read_capability_fd("--runner-token-fd")
control_domain = (
    arguments[arguments.index("--control-token-domain") + 1]
    if "--control-token-domain" in arguments
    else None
)
if command in OWNER_CONTROL_COMMANDS:
    assert control_token is not None
    assert control_domain == "owner"
    assert runner_token is None
    assert all(name not in os.environ for name in PRIVILEGED_TOKEN_ENV_NAMES)
control_envelope = None
if command in {
    "control-capability-bind",
    "stale-recovery-capability-prepare",
    "cadence-admit",
    "cadence-close",
    "stale-turn-reconcile",
    "review-drive",
    "context-handoff-prepare",
    "review-status",
}:
    control_envelope = json.loads(sys.stdin.read())
review_db = os.environ.get("RETHLAS_REVIEW_DB")
mock_root = (
    pathlib.Path(review_db).resolve().parents[2]
    if review_db
    else pathlib.Path(__file__).resolve().parents[1]
)
calls_path = pathlib.Path(
    os.environ.get("MOCK_CADENCE_CALLS_FILE", mock_root / "cadence-calls.jsonl")
)
with calls_path.open("a", encoding="utf-8") as handle:
    handle.write(canonical({
        "argv": arguments,
        "command": command,
        "control_capability": (
            {
                "domain": control_domain,
                "sha256": hashlib.sha256(control_token.encode("ascii")).hexdigest(),
            }
            if control_token is not None
            else None
        ),
        "control_envelope": control_envelope,
        "capability_env_present": any(
            name in os.environ for name in PRIVILEGED_TOKEN_ENV_NAMES
        ),
        "runner_capability_sha256": (
            hashlib.sha256(runner_token.encode("ascii")).hexdigest()
            if runner_token is not None
            else None
        ),
        "raw_cost_policy_present": "RETHLAS_COST_GATE_POLICY" in os.environ,
        "resolved_cost_policy_sha256": os.environ.get(
            "RETHLAS_RESOLVED_COST_POLICY_SHA256"
        ),
    }) + "\n")

if command == "policy-contract":
    reported_review = dict(REVIEW)
    guardian_mode = os.environ.get("MOCK_GUARDIAN_ENFORCEMENT_READY_MODE", "ready")
    if guardian_mode == "false":
        reported_review["guardian_enforcement_ready"] = False
    elif guardian_mode == "missing":
        del reported_review["guardian_enforcement_ready"]
    elif guardian_mode == "non_boolean":
        reported_review["guardian_enforcement_ready"] = "true"
    elif guardian_mode != "ready":
        raise AssertionError("unsupported guardian release-gate mock mode")
    reported_review_digest = hashlib.sha256(canonical(reported_review).encode()).hexdigest()
    reported_material = {
        "schema_version": "rethlas-policy-contract-v1",
        "review_cadence_policy": {
            **reported_review,
            "policy_sha256": reported_review_digest,
        },
        "continuous_supervisor_policy": contract_material[
            "continuous_supervisor_policy"
        ],
        "context_guard_policy": contract_material["context_guard_policy"],
    }
    reported_contract = {
        **reported_material,
        "contract_sha256": hashlib.sha256(
            canonical(reported_material).encode()
        ).hexdigest(),
    }
    if os.environ.get("MOCK_TAMPER_GUARDIAN_POLICY_DIGEST"):
        reported_contract["review_cadence_policy"]["policy_sha256"] = "0" * 64
    print(canonical(reported_contract))
    raise SystemExit(0)

state_path = pathlib.Path(
    os.environ.get("MOCK_CADENCE_STATE_FILE", mock_root / "cadence-state.json")
)
if control_envelope is None:
    run_id = arguments[arguments.index("--run-id") + 1]
elif "run_id" in control_envelope["payload"]:
    run_id = control_envelope["payload"]["run_id"]
elif isinstance(control_envelope["payload"].get("assertions"), dict):
    run_id = control_envelope["payload"]["assertions"]["run_id"]
else:
    run_id = json.loads(state_path.read_text(encoding="utf-8"))["run_id"]
if command == "init":
    problem_id = arguments[arguments.index("--problem-id") + 1]
    if not state_path.exists():
        state_path.write_text(
            canonical({
                "disposition": "initial_start_allowed",
                "cycle_history": [],
                "cycle_id": None,
                "cycle_serial": 0,
                "codex_digests": [],
                "capability_revision": 0,
                "generation_control_instances": [],
                "generation": 0,
                "guardian_clock_sha256": None,
                "helper_digests": [],
                "helper_paths": [],
                "memory_batch_publications": {},
                "mock_publication": os.environ.get("MOCK_PUBLICATION"),
                "review_driver_digests": [],
                "review_driver_package_digests": [],
                "review_driver_paths": [],
                "problem_id": problem_id,
                "paid_root_count": 0,
                "run_count": 0,
                "run_id": run_id,
                "runtime_digests": [],
                "thread_epoch": None,
                "token_digests": [],
            }),
            encoding="utf-8",
        )
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["run_id"] == run_id
    assert state["problem_id"] == problem_id
    print(canonical({"run_id": run_id, "problem_id": problem_id}))
    raise SystemExit(0)

state = json.loads(state_path.read_text(encoding="utf-8"))

if command == "status":
    thread_epoch = state.get("thread_epoch")
    thread_id = None
    active_turn_id = None
    if isinstance(thread_epoch, dict):
        thread_id = thread_epoch.get("thread_id")
        active_turn_id = thread_epoch.get("active_turn_id")
    print(canonical({
        "active_turn_id": active_turn_id,
        "generation": state.get("generation", state.get("run_count", 0)),
        "generator_fingerprint": None,
        "head_digest": "0" * 64,
        "last_sequence": 0,
        "message_counts": {},
        "message_source_counts": {},
        "problem_id": state["problem_id"],
        "quarantine": state.get("quarantine"),
        "run_id": run_id,
        "thread_id": thread_id,
        "turn_intent_counts": {},
    }))
    raise SystemExit(0)


def active_cycle_id() -> str:
    cycle_id = state.get("cycle_id")
    assert isinstance(cycle_id, str)
    assert cycle_id.startswith("cycle_") and len(cycle_id) == 38
    assert all(character in "0123456789abcdef" for character in cycle_id[6:])
    return cycle_id


def cadence_projection() -> dict[str, object]:
    disposition = state["disposition"]
    projected_review = dict(REVIEW)
    if os.environ.get("MOCK_GUARDIAN_ENFORCEMENT_READY_MODE") == "false":
        projected_review["guardian_enforcement_ready"] = False
    projected_review_digest = hashlib.sha256(
        canonical(projected_review).encode()
    ).hexdigest()
    adapter_resume_allowed = disposition in {
        "initial_start_allowed",
        "continue_active_cycle",
        "continue_review_only",
        "continue_next_cycle",
        "continue_reviewed_cycle_fresh_epoch",
        "resume_active_cycle",
        "terminal_observed_pending_finalization",
        "review_boundary_recovery_required",
        "continuous_review_host_recovery",
        "continuous_intent_successor_required",
        "continuous_verdict_successor_required",
    }
    paid_turn_allowed = disposition in {
        "initial_start_allowed",
        "continue_active_cycle",
        "continue_review_only",
        "continue_next_cycle",
        "continue_reviewed_cycle_fresh_epoch",
        "continuous_intent_successor_required",
        "continuous_verdict_successor_required",
    }
    projected_epoch = state["thread_epoch"]
    if (
        disposition == "continue_next_cycle"
        and os.environ.get("MOCK_CORRUPT_CONTINUE_EPOCH")
    ):
        projected_epoch = {**projected_epoch, "handoff_sha256": "f" * 64}
    if (
        isinstance(projected_epoch, dict)
        and projected_epoch.get("state") == "pending"
        and os.environ.get("MOCK_CORRUPT_OWNER_YIELD_HANDOFF")
    ):
        projected_epoch = {**projected_epoch, "handoff_sha256": "e" * 64}
    review_state = "not_started" if state["run_count"] == 0 else "active"
    review_projection: dict[str, object] = {
        "continuation": (
            {
                "authorization_id": "cadauth_" + "a" * 32,
                "expires_at": 9_999_999_999.0,
                "mode": (
                    "active_cycle"
                    if disposition == "continue_active_cycle"
                    else "review_only"
                ),
                "reserved": False,
                "review_action_id": (
                    None
                    if disposition == "continue_active_cycle"
                    else "action_mock_review"
                ),
                "state": "prepared",
                "superseded": False,
            }
            if disposition in {"continue_active_cycle", "continue_review_only"}
            else None
        ),
        "review_boundary": (
            {
                "boundary_id": "reviewbound_" + "b" * 32,
                "no_live_descendants_sha256": (
                    "d" * 64
                    if disposition
                    in {"review_drive_required", "post_review_handoff_required"}
                    else None
                ),
                "review_ordinal": 1,
                "root_terminal_sha256": "c" * 64,
                "root_thread_id": "thread_mock_1",
                "root_turn_id": "turn_mock_1",
                "state": (
                    "descendants_terminal"
                    if disposition
                    in {"review_drive_required", "post_review_handoff_required"}
                    else "root_terminal"
                ),
            }
            if disposition
            in {
                "review_drive_required",
                "post_review_handoff_required",
                "review_boundary_recovery_required",
            }
            else None
        ),
        "policy_digest": (
            continuous_digest
            if os.environ.get("MOCK_EXPECT_REVIEW_POLICY")
            == CONTINUOUS["policy_id"]
            else projected_review_digest
        ),
        "policy_id": os.environ.get(
            "MOCK_EXPECT_REVIEW_POLICY", REVIEW["policy_id"]
        ),
        "state": review_state,
    }
    if state.get("cycle_id") is not None:
        review_projection["cycle_id"] = active_cycle_id()
        review_projection["generation"] = int(state["generation"])
        review_projection["guardian_clock_sha256"] = state[
            "guardian_clock_sha256"
        ]
        review_projection["allowed_action"] = state.get(
            "allowed_action",
            os.environ.get("MOCK_CADENCE_ALLOWED_ACTION", "free_construction"),
        )
    return {
        "context_guard": {
            "adapter_resume_allowed": adapter_resume_allowed,
            "emergency_marker": None,
            "operational_failures": [],
            "pending_terminal": None,
            "policy_digest": context_digest,
            "policy_id": CONTEXT["policy_id"],
            "state": "not_started" if state["run_count"] == 0 else "active",
        },
        "continuous_supervisor": None,
        "disposition": disposition,
        "paid_turn_allowed": paid_turn_allowed,
        "quarantine": state.get("quarantine"),
        "review_cadence": review_projection,
        "run_id": run_id,
        "thread_epoch": projected_epoch,
    }


if command == "cadence-control-state":
    if os.environ.get("MOCK_ABSOLUTE_DEADLINE_EXPIRED"):
        state["disposition"] = "hard_stopped_unfinalized"
        state_path.write_text(canonical(state), encoding="utf-8")
    if os.environ.get("MOCK_MUTATE_REVIEW_HELPER_DURING_PREFLIGHT"):
        helper_source = pathlib.Path(os.environ["MOCK_REVIEW_HELPER_SOURCE"])
        if not os.environ.get("MOCK_REVIEW_HELPER_MUTATED_MARKER"):
            with helper_source.open("a", encoding="utf-8") as handle:
                handle.write("\n# mutated before reviewer/root spawn\n")
    if os.environ.get("MOCK_MUTATE_REVIEW_DRIVER_PACKAGE_DURING_PREFLIGHT"):
        driver_source = pathlib.Path(os.environ["MOCK_REVIEW_DRIVER_PACKAGE_SOURCE"])
        if not state.get("review_driver_package_mutated"):
            with driver_source.open("a", encoding="utf-8") as handle:
                handle.write("\n# mutated driver dependency before reviewer/root spawn\n")
            state["review_driver_package_mutated"] = True
            state_path.write_text(canonical(state), encoding="utf-8")
    if os.environ.get("MOCK_MUTATE_RECURSIVE_SKILL_DURING_PREFLIGHT"):
        skill_source = pathlib.Path(os.environ["MOCK_RECURSIVE_SKILL_SOURCE"])
        if not state.get("recursive_skill_mutated"):
            with skill_source.open("a", encoding="utf-8") as handle:
                handle.write("\n# mutated cost policy before reviewer/root spawn\n")
            state["recursive_skill_mutated"] = True
            state_path.write_text(canonical(state), encoding="utf-8")
    if os.environ.get("MOCK_MUTATE_CODEX_DURING_PREFLIGHT"):
        codex_source = pathlib.Path(os.environ["MOCK_CODEX_SOURCE"])
        if not state.get("codex_mutated"):
            with codex_source.open("a", encoding="utf-8") as handle:
                handle.write("\n# mutated before root/reviewer spawn\n")
            state["codex_mutated"] = True
            state_path.write_text(canonical(state), encoding="utf-8")
    print(canonical(cadence_projection()))
    raise SystemExit(0)

if command == "stale-recovery-capability-prepare":
    assert control_envelope is not None
    assert control_envelope["schema_version"] == "rethlas_review_adapter_command_v1"
    assert control_envelope["command"] == "stale_recovery_capability_prepare"
    payload = control_envelope["payload"]
    assert set(payload) == {
        "operation",
        "run_id",
        "expected_thread_id",
        "expected_turn_id",
        "source_database_path",
        "source_database_sha256",
        "source_preimage_manifest_sha256",
        "copy_database_device",
        "copy_database_inode",
        "copy_database_preimage_sha256",
        "owner_uid",
        "database_mode_octal",
        "codex_bin",
        "codex_bin_sha256",
    }
    assert payload["operation"] == "stale_recovery_capability_prepare"
    assert payload["run_id"] == run_id
    assert payload["expected_thread_id"] == state["thread_epoch"]["thread_id"]
    assert payload["expected_turn_id"] == state["thread_epoch"]["active_turn_id"]
    source_path = pathlib.Path(payload["source_database_path"])
    copy_path = pathlib.Path(arguments[arguments.index("--db") + 1])
    assert source_path.is_absolute() and copy_path.is_absolute()
    assert source_path.stat().st_ino != copy_path.stat().st_ino
    assert hashlib.sha256(source_path.read_bytes()).hexdigest() == payload[
        "source_database_sha256"
    ]
    assert len(payload["source_preimage_manifest_sha256"]) == 64
    assert all(
        character in "0123456789abcdef"
        for character in payload["source_preimage_manifest_sha256"]
    )
    assert hashlib.sha256(copy_path.read_bytes()).hexdigest() == payload[
        "copy_database_preimage_sha256"
    ]
    assert copy_path.stat().st_dev == payload["copy_database_device"]
    assert copy_path.stat().st_ino == payload["copy_database_inode"]
    assert payload["owner_uid"] == os.getuid()
    assert payload["database_mode_octal"] == "0600"
    codex_path = pathlib.Path(payload["codex_bin"])
    assert codex_path.is_absolute() and codex_path.is_file()
    assert hashlib.sha256(codex_path.read_bytes()).hexdigest() == payload[
        "codex_bin_sha256"
    ]
    token = os.environ.get("RETHLAS_STALE_RECOVERY_TOKEN", "")
    assert len(token) == 64 and all(character in "0123456789abcdef" for character in token)
    for forbidden in (
        "RETHLAS_REVIEW_CONTROL_TOKEN",
        "RETHLAS_GUARDIAN_CYCLE_TOKEN",
        "RETHLAS_RUNNER_CYCLE_TOKEN",
    ):
        assert not os.environ.get(forbidden)
    state["stale_recovery_token_sha256"] = hashlib.sha256(token.encode()).hexdigest()
    state_path.write_text(canonical(state), encoding="utf-8")
    seed = {
        "schema_version": "rethlas_stale_recovery_capability_v1",
        "operation": "stale_recovery_capability_prepare",
        "capability_id": "stalecap_" + "5" * 32,
        "run_id": run_id,
        "state": "active",
        "scope": "stale_turn_reconcile",
        "expected_thread_id": payload["expected_thread_id"],
        "expected_turn_id": payload["expected_turn_id"],
        "source_database_sha256": payload["source_database_sha256"],
        "source_preimage_manifest_sha256": payload[
            "source_preimage_manifest_sha256"
        ],
        "source_sidecars": {
            "wal_size": pathlib.Path(str(source_path) + "-wal").stat().st_size
            if pathlib.Path(str(source_path) + "-wal").exists()
            else 0,
            "shm_size": pathlib.Path(str(source_path) + "-shm").stat().st_size
            if pathlib.Path(str(source_path) + "-shm").exists()
            else 0,
        },
        "backup_manifest_sha256": "6" * 64,
        "copy_database_device": payload["copy_database_device"],
        "copy_database_inode": payload["copy_database_inode"],
        "copy_database_preimage_sha256": payload["copy_database_preimage_sha256"],
        "codex_bin": payload["codex_bin"],
        "codex_bin_sha256": payload["codex_bin_sha256"],
        "created_sequence": 10,
    }
    seed["receipt_sha256"] = hashlib.sha256(canonical(seed).encode()).hexdigest()
    if os.environ.get("MOCK_TAMPER_STALE_PREPARE_RECEIPT"):
        seed["receipt_sha256"] = "0" * 64
    print(canonical(seed))
    raise SystemExit(0)

if command == "stale-turn-reconcile":
    assert control_envelope is not None
    assert control_envelope["schema_version"] == "rethlas_review_adapter_command_v1"
    assert control_envelope["command"] == "stale_turn_reconcile"
    payload = control_envelope["payload"]
    assert set(payload) == {
        "operation",
        "run_id",
        "expected_thread_id",
        "expected_turn_id",
    }
    assert payload["operation"] == "stale_turn_reconcile"
    assert payload["run_id"] == run_id
    assert payload["expected_thread_id"] == state["thread_epoch"]["thread_id"]
    assert payload["expected_turn_id"] == state["thread_epoch"]["active_turn_id"]
    token = os.environ.get("RETHLAS_STALE_RECOVERY_TOKEN", "")
    assert hashlib.sha256(token.encode()).hexdigest() == state[
        "stale_recovery_token_sha256"
    ]
    for forbidden in (
        "RETHLAS_REVIEW_CONTROL_TOKEN",
        "RETHLAS_GUARDIAN_CYCLE_TOKEN",
        "RETHLAS_RUNNER_CYCLE_TOKEN",
    ):
        assert not os.environ.get(forbidden)
    state["thread_epoch"]["active_turn_id"] = None
    state["disposition"] = "operational_blocked"
    state["quarantine"] = {
        "kind": "adapter_loss_terminal_discontinuity",
        "reason": "mock terminal discontinuity",
        "thread_id": payload["expected_thread_id"],
        "turn_id": payload["expected_turn_id"],
    }
    state_path.write_text(canonical(state), encoding="utf-8")
    result = {
        "schema_version": "rethlas_stale_turn_reconcile_result_v1",
        "operation": "stale_turn_reconcile",
        "run_id": run_id,
        "thread_id": payload["expected_thread_id"],
        "turn_id": payload["expected_turn_id"],
        "state": "terminal_reconciled_quarantined",
        "observed_status": "interrupted",
        "thread_read_response_sha256": "1" * 64,
        "turn_sha256": "2" * 64,
        "terminal_sha256": "3" * 64,
        "settled_message_count": 4,
        "settled_messages_sha256": "4" * 64,
        "committed_sequence": 11,
    }
    result["receipt_sha256"] = hashlib.sha256(canonical(result).encode()).hexdigest()
    print(canonical(result))
    raise SystemExit(0)

if command == "control-capability-bind":
    assert control_envelope is not None
    assert control_envelope["schema_version"] == "rethlas_review_adapter_command_v1"
    assert control_envelope["command"] == "control_capability_bind"
    payload = control_envelope["payload"]
    assert set(payload) == {
        "run_id",
        "contract_cli_path",
        "contract_cli_sha256",
        "trusted_runtime_sha256",
        "review_driver_path",
        "review_driver_sha256",
        "review_driver_package_sha256",
        "expected_model",
        "reasoning_effort",
        "review_policy_sha256",
        "codex_bin",
        "codex_bin_sha256",
        "generation_control_instance_id",
        "expected_statement_sha256",
    }
    helper_path = pathlib.Path(payload["contract_cli_path"])
    driver_path = pathlib.Path(payload["review_driver_path"])
    codex_path = pathlib.Path(payload["codex_bin"])
    assert helper_path.is_absolute() and helper_path.is_file()
    assert hashlib.sha256(helper_path.read_bytes()).hexdigest() == payload[
        "contract_cli_sha256"
    ]
    assert driver_path.is_absolute() and driver_path.is_file()
    driver_commitment = review_driver_commitment(driver_path)
    assert payload["review_driver_sha256"] == driver_commitment["driver_sha256"]
    assert payload["review_driver_package_sha256"] == driver_commitment[
        "package_sha256"
    ]
    assert codex_path.is_absolute() and codex_path.is_file() and not codex_path.is_symlink()
    assert hashlib.sha256(codex_path.read_bytes()).hexdigest() == payload[
        "codex_bin_sha256"
    ]
    assert payload["expected_model"] == "gpt-5.6-sol"
    assert payload["reasoning_effort"] == "max"
    assert payload["review_policy_sha256"] == (
        continuous_digest
        if os.environ.get("MOCK_EXPECT_REVIEW_POLICY") == CONTINUOUS["policy_id"]
        else review_digest
    )
    assert payload["expected_statement_sha256"] == os.environ[
        "RETHLAS_EXPECTED_STATEMENT_SHA256"
    ]
    assert len(payload["generation_control_instance_id"]) == 32
    if state["disposition"] == "owner_yield_close_required" and state[
        "generation_control_instances"
    ]:
        # A restart may rotate the master capability/path, but it must keep the
        # exact prior generation instance until cadence-close consumes the
        # already-written wait receipt.
        assert payload["generation_control_instance_id"] == state[
            "generation_control_instances"
        ][-1]
    assert control_token is not None
    token_sha256 = hashlib.sha256(control_token.encode("ascii")).hexdigest()
    binding_state = "bound" if not state["token_digests"] else "rotated"
    state["capability_revision"] = int(state.get("capability_revision", 0)) + 1
    state["token_digests"].append(token_sha256)
    state["helper_paths"].append(str(helper_path))
    state["helper_digests"].append(payload["contract_cli_sha256"])
    state["runtime_digests"].append(payload["trusted_runtime_sha256"])
    state["review_driver_paths"].append(str(driver_path))
    state["review_driver_digests"].append(payload["review_driver_sha256"])
    state["review_driver_package_digests"].append(
        payload["review_driver_package_sha256"]
    )
    state["codex_digests"].append(payload["codex_bin_sha256"])
    state["generation_control_instances"].append(
        payload["generation_control_instance_id"]
    )
    state_path.write_text(canonical(state), encoding="utf-8")
    print(canonical({
        "schema_version": "rethlas_control_capability_binding_v1",
        "run_id": run_id,
        "state": binding_state,
        "capability_revision": state["capability_revision"],
        "token_sha256": token_sha256,
        "contract_cli_sha256": payload["contract_cli_sha256"],
        "trusted_runtime_sha256": payload["trusted_runtime_sha256"],
        "review_driver_sha256": payload["review_driver_sha256"],
        "review_driver_package_sha256": payload[
            "review_driver_package_sha256"
        ],
        "generation_control_instance_id": payload["generation_control_instance_id"],
    }))
    raise SystemExit(0)

if command == "cadence-admit":
    assert control_envelope is not None
    assert control_envelope["schema_version"] == "rethlas_review_adapter_command_v1"
    assert control_envelope["command"] == "cadence_admit"
    payload = control_envelope["payload"]
    assert set(payload) == {"operation", "run_id", "generation_control_receipt"}
    assert control_token is not None
    assert hashlib.sha256(control_token.encode("ascii")).hexdigest() == state[
        "token_digests"
    ][-1]
    receipt = payload["generation_control_receipt"]
    assert receipt["control"]["state"] == "running"
    assert receipt["control"]["reason"] == "owner_runner_started"
    assert receipt["control"]["instance_id"] == state[
        "generation_control_instances"
    ][-1]
    operation = payload["operation"]
    if operation == "continue_active_cycle":
        assert state["disposition"] == "continuation_authorization_required"
        state["prior_allowed_action"] = state.get(
            "allowed_action",
            os.environ.get("MOCK_CADENCE_ALLOWED_ACTION", "free_construction"),
        )
        state["allowed_action"] = "continue_active_cycle_authorized"
        state["disposition"] = "continue_active_cycle"
    elif operation == "continue_review_only":
        assert state["disposition"] == "review_turn_authorization_required"
        state["disposition"] = "continue_review_only"
    elif operation == "owner_resume":
        assert state["disposition"] in {"owner_wait_cost", "owner_wait_advisor"}
        assert state["thread_epoch"]["state"] == "pending"
        if os.environ.get("MOCK_FAIL_BEFORE_OWNER_RESUME_CAS"):
            raise SystemExit(75)
        state["disposition"] = "continue_next_cycle"
    else:
        raise AssertionError(f"unsupported mock cadence admission {operation}")
    state_path.write_text(canonical(state), encoding="utf-8")
    if (
        operation == "continue_active_cycle"
        and os.environ.get("MOCK_FAIL_WRAPPER_AFTER_ACTIVE_ADMIT")
    ):
        raise SystemExit(75)
    print(canonical(cadence_projection()))
    raise SystemExit(0)

if command == "cadence-close":
    assert control_envelope is not None
    assert control_envelope["schema_version"] == "rethlas_review_adapter_command_v1"
    assert control_envelope["command"] == "cadence_close"
    payload = control_envelope["payload"]
    assert control_token is not None
    assert hashlib.sha256(control_token.encode("ascii")).hexdigest() == state[
        "token_digests"
    ][-1]
    if payload.get("operation") == "verified_completion":
        assert set(payload) == {
            "operation",
            "run_id",
            "problem_id",
            "statement_sha256",
            "publication_receipt_sha256",
            "verified_proof_sha256",
            "published_bytes",
        }
        assert payload["run_id"] == run_id
        assert payload["problem_id"] == state["problem_id"]
        assert payload["statement_sha256"] == os.environ[
            "RETHLAS_EXPECTED_STATEMENT_SHA256"
        ]
        receipt_path = pathlib.Path(os.environ["RETHLAS_RECEIPTS_ROOT"]) / (
            state["problem_id"] + ".json"
        )
        proof_path = pathlib.Path(os.environ["RETHLAS_GENERATION_ROOT"]) / (
            "results/" + state["problem_id"] + "/blueprint_verified.md"
        )
        receipt_raw = receipt_path.read_bytes()
        proof_raw = proof_path.read_bytes()
        assert payload["publication_receipt_sha256"] == hashlib.sha256(
            receipt_raw
        ).hexdigest()
        assert payload["verified_proof_sha256"] == hashlib.sha256(
            proof_raw
        ).hexdigest()
        assert payload["published_bytes"] == len(proof_raw)
        if os.environ.get("MOCK_FAIL_VERIFIED_COMPLETION_CLOSE"):
            raise SystemExit(75)
        state["disposition"] = "completed"
        state_path.write_text(canonical(state), encoding="utf-8")
        print(canonical(cadence_projection()))
        raise SystemExit(0)
    assert set(payload) == {
        "operation",
        "run_id",
        "cycle_id",
        "handoff_id",
        "content_sha256",
        "to_thread_epoch",
        "generation_control_receipt",
    }
    assert payload["operation"] == "owner_yield"
    assert state["disposition"] == "owner_yield_close_required"
    epoch = state["thread_epoch"]
    assert payload["cycle_id"] == active_cycle_id()
    assert payload["handoff_id"] == epoch["handoff_id"]
    assert payload["content_sha256"] == epoch["handoff_sha256"]
    assert payload["to_thread_epoch"] == epoch["thread_epoch"]
    wait_state = payload["generation_control_receipt"]["control"]["state"]
    assert payload["generation_control_receipt"]["control"]["instance_id"] == state[
        "generation_control_instances"
    ][-1]
    state["disposition"] = {
        "waiting_cost_gate": "owner_wait_cost",
        "waiting_owner_advisor_decision": "owner_wait_advisor",
    }[wait_state]
    state_path.write_text(canonical(state), encoding="utf-8")
    print(canonical(cadence_projection()))
    raise SystemExit(0)

if command in {"review-drive", "guarded-review-drive"}:
    if command == "guarded-review-drive":
        assert control_envelope is None
        assert runner_token is not None
        assert control_token is None
        assert all(name not in os.environ for name in PRIVILEGED_TOKEN_ENV_NAMES)
        owner_token_sha256 = state["token_digests"][-1]
        assert hashlib.sha256(runner_token.encode("ascii")).hexdigest() != (
            owner_token_sha256
        )
        assert all(
            hashlib.sha256(argument.encode("utf-8")).hexdigest()
            != owner_token_sha256
            for argument in arguments
        )
        assert all(
            hashlib.sha256(value.encode("utf-8")).hexdigest()
            != owner_token_sha256
            for value in os.environ.values()
        )
        payload = {
            "operation": "drive_due_review",
            "run_id": arguments[arguments.index("--run-id") + 1],
            "boundary_id": arguments[arguments.index("--boundary-id") + 1],
        }
    else:
        assert control_envelope is not None
        assert control_envelope[
            "schema_version"
        ] == "rethlas_review_adapter_command_v1"
        assert control_envelope["command"] == "review_drive"
        payload = control_envelope["payload"]
        assert control_token is not None
        assert hashlib.sha256(control_token.encode("ascii")).hexdigest() == state[
            "token_digests"
        ][-1]
    assert set(payload) == {"operation", "run_id", "boundary_id"}
    assert payload["operation"] == "drive_due_review"
    assert payload["run_id"] == run_id
    assert state["disposition"] == "review_drive_required"
    assert payload["boundary_id"] == "reviewbound_" + "b" * 32
    review_id = "review_" + "e" * 32
    request_sha256 = "1" * 64
    snapshot_sha256 = "2" * 64
    disposition = {
        "schema_version": "rethlas_review_disposition_v1",
        "review_id": review_id,
        "request_sha256": request_sha256,
        "snapshot_sha256": snapshot_sha256,
        "decision": {
            "effective_verdict": "green",
            "route_id": "route_mock_active",
            "critic_confirmed_progress_ids": [],
        },
        "active_route": {
            "route_id": "route_mock_active",
            "core_bridge": "Mock host-reviewed bridge.",
            "obligations": ["Complete the next exact milestone."],
        },
        "frozen_route_id": None,
        "route_transition_publication_receipt": {
            "schema_version": "rethlas_route_transition_receipt_v1"
        },
        "next_milestone": {
            "description": "Complete the next exact milestone.",
            "test": "The milestone is persisted and independently reviewable.",
        },
        "evidence_record_ids": [],
        "requires_targeted_verification": False,
    }
    if os.environ.get("MOCK_REVIEW_DRIVE_RED"):
        disposition["decision"] = {
            "effective_verdict": "red",
            "route_id": "route_mock_active",
            "critic_confirmed_progress_ids": [],
        }
        disposition["active_route"] = None
        disposition["frozen_route_id"] = "route_mock_active"
        disposition["next_milestone"] = None
    disposition_sha256 = hashlib.sha256(canonical(disposition).encode()).hexdigest()
    if os.environ.get("MOCK_REVIEW_DRIVE_RED"):
        state["allowed_action"] = "recovery_only"
        state["disposition"] = "route_frozen"
    else:
        prior_epoch = state["thread_epoch"]
        assert isinstance(prior_epoch, dict) and prior_epoch["state"] == "active"
        handoff_sha256 = hashlib.sha256(
            f"review-handoff-{payload['boundary_id']}".encode()
        ).hexdigest()
        state["allowed_action"] = "post_review_handoff_required"
        state["disposition"] = "continue_reviewed_cycle_fresh_epoch"
        state["thread_epoch"] = {
            "active_turn_id": None,
            "handoff_id": f"handoff_{handoff_sha256}",
            "handoff_sha256": handoff_sha256,
            "predecessor_epoch": prior_epoch["thread_epoch"],
            "state": "pending",
            "thread_epoch": prior_epoch["thread_epoch"] + 1,
            "thread_id": None,
        }
    state["review_drive_count"] = int(state.get("review_drive_count", 0)) + 1
    state_path.write_text(canonical(state), encoding="utf-8")
    projection = cadence_projection()
    print(canonical({
        "schema_version": "rethlas_review_drive_result_v1",
        "run_id": run_id,
        "boundary_id": payload["boundary_id"],
        "cycle_id": active_cycle_id(),
        "review_id": review_id,
        "state": "disposition_ready",
        "disposition_sha256": disposition_sha256,
        "disposition": disposition,
        "review_cadence": projection["review_cadence"],
        "thread_epoch": projection["thread_epoch"],
    }))
    raise SystemExit(0)

if command == "context-handoff-prepare":
    assert control_envelope is not None
    assert control_envelope["schema_version"] == "rethlas_review_adapter_command_v1"
    assert control_envelope["command"] == "context_handoff_prepare"
    payload = control_envelope["payload"]
    assert set(payload) == {"operation", "purpose", "proposal", "assertions"}
    assert payload["operation"] == "context_handoff_prepare"
    assert payload["purpose"] == "owner_yield"
    proposal = payload["proposal"]
    assertions = payload["assertions"]
    assert set(proposal) == {
        "active_route", "new_record_ids", "obligations", "next_action", "pending"
    }
    assert set(assertions) == {
        "run_id", "problem_id", "statement_sha256", "blueprint_sha256",
        "last_review", "yellow_streak", "route_frozen",
    }
    content = {
        "schema_version": "rethlas_context_handoff_v3",
        "purpose": "owner_yield",
        "run_id": assertions["run_id"],
        "problem_id": assertions["problem_id"],
        "from_thread_epoch": "1",
        "statement_sha256": assertions["statement_sha256"],
        "blueprint_sha256": assertions["blueprint_sha256"],
        "cadence": {
            "phase": "work_0_60",
            "cycle_started_at_utc": "2026-08-11T00:00:00+00:00",
            "minute60_at_utc": "2026-08-11T01:00:00+00:00",
            "minute120_at_utc": "2026-08-11T02:00:00+00:00",
            "close_at_utc": "2026-08-11T02:27:00+00:00",
            "hard_stop_at_utc": "2026-08-11T02:30:00+00:00",
        },
        "active_route": proposal["active_route"],
        "last_review": assertions["last_review"],
        "new_record_ids": proposal["new_record_ids"],
        "yellow_streak": assertions["yellow_streak"],
        "route_frozen": assertions["route_frozen"],
        "pending": proposal["pending"],
        "obligations": proposal["obligations"],
        "next_action": proposal["next_action"],
    }
    content_sha256 = hashlib.sha256(canonical(content).encode()).hexdigest()
    handoff_id = f"handoff_{content_sha256}"
    state["owner_yield_handoff"] = {
        "handoff_id": handoff_id,
        "content_sha256": content_sha256,
        "to_thread_epoch": 2,
        "root_thread_id": "thread_mock_1",
        "root_turn_id": "turn_mock_1",
    }
    state_path.write_text(canonical(state), encoding="utf-8")
    print(canonical({
        "schema_version": "rethlas_review_adapter_response_v1",
        "operation": "context_handoff_prepare",
        "handoff_id": handoff_id,
        "content_sha256": content_sha256,
        "state": "prepared",
        "idempotent": False,
        "content": content,
        "binding": None,
    }))
    raise SystemExit(0)

if command == "review-status":
    assert control_envelope is not None
    assert control_envelope["schema_version"] == "rethlas_review_adapter_command_v1"
    assert control_envelope["command"] == "review_status"
    payload = control_envelope["payload"]
    operation = payload.get("operation")
    if operation == "memory_batch_publication_commit":
        assert set(payload) == {
            "operation", "problem_id", "batch_id", "checkpoint_sha256",
            "commit_sha256", "publication_class",
        }
        assert payload["problem_id"] == state["problem_id"]
        assert payload["batch_id"].startswith("batch_")
        assert len(payload["batch_id"]) == 70
        assert all(
            character in "0123456789abcdef"
            for character in payload["batch_id"][6:]
        )
        for digest_name in ("checkpoint_sha256", "commit_sha256"):
            assert len(payload[digest_name]) == 64
            assert all(
                character in "0123456789abcdef"
                for character in payload[digest_name]
            )
        assert payload["publication_class"] in {
            "reasoning_checkpoint", "control_only"
        }
        publications = state.setdefault("memory_batch_publications", {})
        existing = publications.get(payload["batch_id"])
        request_bindings = {
            key: payload[key]
            for key in (
                "problem_id", "batch_id", "checkpoint_sha256",
                "commit_sha256", "publication_class",
            )
        }
        if existing is not None:
            assert all(
                existing[key] == value
                for key, value in request_bindings.items()
            )
            receipt = existing
        else:
            receipt_seed = {
                "schema_version":
                    "rethlas_memory_batch_publication_receipt_v1",
                "state": "accepted",
                "run_id": run_id,
                **request_bindings,
                "cycle_id": active_cycle_id(),
                "cutoff_action_id": "cadact_" + "a" * 32,
                "cutoff_kind": "review_1",
                "cutoff_at_utc": "2030-01-01T00:30:00+00:00",
                "cutoff_monotonic": 2.0e18,
                "accepted_at_utc": "2030-01-01T00:00:00+00:00",
                "accepted_at_monotonic": 1.0,
                "boot_identity": "mock-cadence-boot",
            }
            receipt = {
                **receipt_seed,
                "receipt_sha256": hashlib.sha256(
                    canonical(receipt_seed).encode("utf-8")
                ).hexdigest(),
            }
            # The fake adapter is invoked concurrently by three independent
            # MCP processes.  Merge under a sidecar lock so a later process
            # cannot overwrite a receipt committed by an earlier snapshot.
            lock_path = state_path.with_suffix(state_path.suffix + ".lock")
            lock_path.touch(exist_ok=True)
            with lock_path.open("r+") as lock_handle:
                import fcntl

                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
                latest = json.loads(state_path.read_text(encoding="utf-8"))
                latest_publications = latest.setdefault(
                    "memory_batch_publications", {}
                )
                winner = latest_publications.get(payload["batch_id"])
                if winner is not None:
                    assert all(
                        winner[key] == value
                        for key, value in request_bindings.items()
                    )
                    receipt = winner
                else:
                    latest_publications[payload["batch_id"]] = receipt
                    state_path.write_text(canonical(latest), encoding="utf-8")
        print(canonical(receipt))
        raise SystemExit(0)
    if operation == "memory_batch_publication_status":
        assert set(payload) == {"operation", "problem_id"}
        assert payload["problem_id"] == state["problem_id"]
        latest = json.loads(state_path.read_text(encoding="utf-8"))
        publications = latest.setdefault("memory_batch_publications", {})
        receipts = [
            publications[batch_id]
            for batch_id in sorted(publications)
            if publications[batch_id]["state"] == "accepted"
        ]
        print(canonical({
            "schema_version": "rethlas_memory_batch_publication_status_v1",
            "run_id": run_id,
            "problem_id": payload["problem_id"],
            "receipts": receipts,
        }))
        raise SystemExit(0)
    assert set(payload) == {
        "operation", "state", "reason_sha256", "evidence_record_ids"
    }
    assert payload["operation"] == "generation_yield_prepare"
    handoff = state["owner_yield_handoff"]
    print(canonical({
        "schema_version": "rethlas_generation_yield_admission_v1",
        "operation": "generation_yield_prepare",
        "admission_id": "yieldadmit_mock_1",
        "run_id": run_id,
        "cycle_id": active_cycle_id(),
        "handoff_id": handoff["handoff_id"],
        "content_sha256": handoff["content_sha256"],
        "to_thread_epoch": handoff["to_thread_epoch"],
        "root_thread_id": handoff["root_thread_id"],
        "root_turn_id": handoff["root_turn_id"],
        "state": payload["state"],
        "reason_sha256": payload["reason_sha256"],
        "evidence_record_ids": payload["evidence_record_ids"],
    }))
    raise SystemExit(0)

starting_disposition = state["disposition"]
if starting_disposition == "continue_active_cycle":
    assert state["allowed_action"] == "continue_active_cycle_authorized"
    state["allowed_action"] = state.pop(
        "prior_allowed_action", "free_construction"
    )
if starting_disposition in {"initial_start_allowed", "continue_next_cycle"}:
    prior_cycle_id = state.get("cycle_id")
    state["cycle_serial"] = int(state.get("cycle_serial", 0)) + 1
    state["generation"] = int(state.get("generation", 0)) + 1
    state["cycle_id"] = f"cycle_{state['cycle_serial']:032x}"
    state["guardian_clock_sha256"] = hashlib.sha256(
        f"guardian-clock-{state['cycle_serial']}".encode("ascii")
    ).hexdigest()
    assert state["cycle_id"] != prior_cycle_id
    state.setdefault("cycle_history", []).append(state["cycle_id"])
    state_path.write_text(canonical(state), encoding="utf-8")
if starting_disposition == "continue_reviewed_cycle_fresh_epoch":
    reviewed_epoch = state["thread_epoch"]
    assert reviewed_epoch["state"] == "pending"
    assert reviewed_epoch["thread_id"] is None
    prompt = arguments[arguments.index("--prompt") + 1]
    assert prompt.startswith("[TRUSTED HOST REHYDRATION REQUIRED]")
    state["allowed_action"] = os.environ.get(
        "MOCK_REVIEWED_ALLOWED_ACTION", "continue_to_next_milestone"
    )
    state["reviewed_handoff_consumed_count"] = int(
        state.get("reviewed_handoff_consumed_count", 0)
    ) + 1
    state_path.write_text(canonical(state), encoding="utf-8")
if starting_disposition in {
    "initial_start_allowed",
    "continue_active_cycle",
    "continue_next_cycle",
    "continue_reviewed_cycle_fresh_epoch",
    "continuous_intent_successor_required",
    "continuous_verdict_successor_required",
}:
    state["paid_root_count"] = int(state.get("paid_root_count", 0)) + 1
    state_path.write_text(canonical(state), encoding="utf-8")
if (
    starting_disposition == "continue_reviewed_cycle_fresh_epoch"
    and os.environ.get("MOCK_FAIL_AFTER_REVIEWED_EPOCH_DISPATCH")
):
    state["run_count"] = int(state["run_count"]) + 1
    state["disposition"] = "terminal_observed_pending_finalization"
    state_path.write_text(canonical(state), encoding="utf-8")
    raise SystemExit(75)

assert arguments[arguments.index("--review-cadence-policy") + 1] == os.environ.get(
    "MOCK_EXPECT_REVIEW_POLICY", REVIEW["policy_id"]
)
assert arguments[arguments.index("--context-guard-policy") + 1] == CONTEXT["policy_id"]
assert arguments[arguments.index("--policy-contract-sha256") + 1] == contract["contract_sha256"]
assert runner_token is not None
assert control_token is None
assert all(name not in os.environ for name in PRIVILEGED_TOKEN_ENV_NAMES)
owner_token_sha256 = state["token_digests"][-1]
assert all(
    hashlib.sha256(argument.encode("utf-8")).hexdigest() != owner_token_sha256
    for argument in arguments
)
assert all(
    hashlib.sha256(value.encode("utf-8")).hexdigest() != owner_token_sha256
    for value in os.environ.values()
)
helper_path = pathlib.Path(
    arguments[arguments.index("--review-contract-cli-path") + 1]
)
helper_sha256 = arguments[arguments.index("--review-contract-cli-sha256") + 1]
driver_path = pathlib.Path(arguments[arguments.index("--review-driver-path") + 1])
driver_sha256 = arguments[arguments.index("--review-driver-sha256") + 1]
driver_package_sha256 = arguments[
    arguments.index("--review-driver-package-sha256") + 1
]
runtime_sha256 = arguments[arguments.index("--trusted-runtime-sha256") + 1]
codex_path = pathlib.Path(arguments[arguments.index("--codex-bin") + 1])
codex_sha256 = arguments[arguments.index("--codex-bin-sha256") + 1]
assert helper_path.is_absolute() and helper_path.is_file() and not helper_path.is_symlink()
assert hashlib.sha256(helper_path.read_bytes()).hexdigest() == helper_sha256
assert driver_path.is_absolute() and driver_path.is_file() and not driver_path.is_symlink()
driver_commitment = review_driver_commitment(driver_path)
assert driver_sha256 == driver_commitment["driver_sha256"]
assert driver_package_sha256 == driver_commitment["package_sha256"]
assert codex_path.is_absolute() and codex_path.is_file() and not codex_path.is_symlink()
assert hashlib.sha256(codex_path.read_bytes()).hexdigest() == codex_sha256
assert hashlib.sha256(runner_token.encode("ascii")).hexdigest() != owner_token_sha256
mcp = tomllib.loads(
    "value=" + arguments[arguments.index("--mcp-config-toml") + 1]
)["value"]
assert set(mcp) == {
    "command",
    "args",
    "cwd",
    "env",
    "required",
    "tool_timeout_sec",
    "default_tools_approval_mode",
}
assert mcp["tool_timeout_sec"] == 3600
assert mcp["required"] is True
assert mcp["default_tools_approval_mode"] == "approve"
selected_review_policy = (
    CONTINUOUS
    if os.environ.get("MOCK_EXPECT_REVIEW_POLICY") == CONTINUOUS["policy_id"]
    else REVIEW
)
selected_review_digest = (
    continuous_digest if selected_review_policy is CONTINUOUS else review_digest
)
for key, expected in (
    ("RETHLAS_REVIEW_CADENCE_POLICY", selected_review_policy["policy_id"]),
    ("RETHLAS_CONTEXT_GUARD_POLICY", CONTEXT["policy_id"]),
    ("RETHLAS_POLICY_CONTRACT_SHA256", contract["contract_sha256"]),
    ("RETHLAS_REVIEW_CONTRACT_CLI_PATH", str(helper_path)),
    ("RETHLAS_REVIEW_CONTRACT_CLI_SHA256", helper_sha256),
    ("RETHLAS_TRUSTED_RUNTIME_SHA256", runtime_sha256),
    ("RETHLAS_REVIEW_ADAPTER_PATH", str(pathlib.Path(__file__).resolve())),
    (
        "RETHLAS_REVIEW_ADAPTER_SHA256",
        hashlib.sha256(pathlib.Path(__file__).read_bytes()).hexdigest(),
    ),
    ("RETHLAS_REVIEW_DB", os.environ["MOCK_CADENCE_EXPECTED_DB"]),
    ("RETHLAS_REVIEW_EXPECTED_MODEL", "gpt-5.6-sol"),
    ("RETHLAS_REVIEW_EXPECTED_REASONING_EFFORT", "max"),
    ("RETHLAS_REVIEW_POLICY_SHA256", selected_review_digest),
    (
        "RETHLAS_RESOLVED_COST_POLICY_JSON",
        os.environ["RETHLAS_RESOLVED_COST_POLICY_JSON"],
    ),
    (
        "RETHLAS_RESOLVED_COST_POLICY_SHA256",
        os.environ["RETHLAS_RESOLVED_COST_POLICY_SHA256"],
    ),
):
    assert mcp["env"][key] == expected
assert "RETHLAS_REVIEW_CONTROL_TOKEN" not in mcp["env"]
scoped_epoch_token = hashlib.sha256(
    f"mock-scoped-epoch:{runner_token}:{state['generation']}".encode("utf-8")
).hexdigest()
assert scoped_epoch_token != runner_token
assert hashlib.sha256(scoped_epoch_token.encode("ascii")).hexdigest() != (
    owner_token_sha256
)
mcp["env"]["RETHLAS_REVIEW_CONTROL_TOKEN"] = scoped_epoch_token
# The real adapter injects this derived epoch capability into the reasoning MCP
# process.  This mock executes the trusted server in-process, so mirror only
# that derived capability here; neither the owner nor runner master token is
# placed in the environment.
os.environ["RETHLAS_REVIEW_CONTROL_TOKEN"] = scoped_epoch_token
mcp_loader_arguments = mcp["args"]
assert mcp_loader_arguments[:3] == ["-I", "-B", "-c"]
mcp_module_arguments = mcp_loader_arguments[4:]
assert len(mcp_module_arguments) == 24
mcp_module_paths = {
    mcp_module_arguments[index]: pathlib.Path(mcp_module_arguments[index + 1])
    for index in range(0, len(mcp_module_arguments), 3)
}

if state.get("mock_publication") == "trusted":
    generation_root = pathlib.Path(os.environ["RETHLAS_GENERATION_ROOT"])
    problem_id = os.environ["RETHLAS_EXPECTED_PROBLEM_ID"]
    verified = generation_root / "results" / problem_id / "blueprint_verified.md"
    verified.parent.mkdir(parents=True, exist_ok=True)
    proof = b"mock verified proof"
    verified.write_bytes(proof)
    snapshot_mcp = mcp_module_paths["mcp.server"].resolve().parent
    sys.path.insert(0, str(snapshot_mcp))
    from proof_context import (
        aggregate_adaptive_context_digest,
        aggregate_context_digest,
        build_item_context,
        extract_verification_target,
        parse_blueprint,
    )
    manifest = parse_blueprint(proof.decode("utf-8"), target_statement="S")
    attestations = []
    for item_id in manifest.item_ids:
        context = build_item_context(manifest, item_id, max_chars=200000)
        attestations.append({
            "item_id": item_id,
            "disposition": "verified",
            "final_round": 0,
            "expanded_proof_ids": [],
            "max_chars": 200000,
            "context_digest": context["digest"],
            "verdict": "correct",
        })
    receipt = pathlib.Path(os.environ["RETHLAS_RECEIPTS_ROOT"]) / (
        problem_id + ".json"
    )
    receipt.parent.mkdir(parents=True, exist_ok=True)
    receipt.write_text(canonical({
        "schema_version": "rethlas-publication-v6",
        "state": "active",
        "problem_id": problem_id,
        "statement_source_digest": os.environ["RETHLAS_EXPECTED_STATEMENT_SHA256"],
        "canonical_target_digest": hashlib.sha256(
            extract_verification_target("S").encode("utf-8")
        ).hexdigest(),
        "proof_digest": hashlib.sha256(proof).hexdigest(),
        "context_digest": aggregate_context_digest(manifest),
        "adaptive_context_digest": aggregate_adaptive_context_digest(
            manifest, attestations
        ),
        "item_context_attestations": attestations,
        "checked_item_ids": list(manifest.item_ids),
        "verified_path": str(verified.absolute()),
        "published_bytes": len(proof),
        "published_at_utc": "2026-08-26T12:00:00+00:00",
        "verification_quorum": 2,
        "verification_passes": [
            {
                "pass_index": 1,
                "verification_attempt_id": "veratt_" + "1" * 32,
                "verifier_run_id": "mock-verifier-run-1",
                "verifier_model": "gpt-5.6-sol",
                "verifier_reasoning_effort": "max",
                "verifier_service_version": "0.3.0",
                "verification_role": "primary",
                "response_sha256": "1" * 64,
                "verdict": "correct",
            },
            {
                "pass_index": 2,
                "verification_attempt_id": "veratt_" + "2" * 32,
                "verifier_run_id": "mock-verifier-run-2",
                "verifier_model": "gpt-5.6-sol",
                "verifier_reasoning_effort": "max",
                "verifier_service_version": "0.3.0",
                "verification_role": "adversarial_full_claim_audit",
                "response_sha256": "2" * 64,
                "verdict": "correct",
            },
        ],
        "supersedes": [],
        "proof_context": {
            "schema_version": "rethlas_publication_proof_context_v3",
            "source_sha256": hashlib.sha256(
                mcp_module_paths[
                    "mcp.publication_proof_context_v3"
                ].read_bytes()
            ).hexdigest(),
            "proof_item_schema_version": 1,
            "proof_context_schema_version": 2,
            "aggregate_context_schema_version": 1,
            "adaptive_aggregate_context_schema_version": 2,
        },
        "verification_limits": {
            "context_max_chars": 200000,
            "max_expansion_rounds": 2,
            "max_expanded_proofs": 8,
            "max_expanded_proof_chars": 200000,
            "max_proof_items": 20000,
            "max_receipt_bytes": 16000000,
            "max_blueprint_bytes": 8000000,
            "max_blueprint_chars": 2000000,
        },
        "publication_target_precondition": {
            "kind": "absent",
            "st_dev": None,
            "st_ino": None,
            "st_size": None,
            "st_mtime_ns": None,
            "content_sha256": None,
        },
    }), encoding="utf-8")

if state["disposition"] == "review_boundary_recovery_required":
    recovery_prompt = arguments[arguments.index("--prompt") + 1]
    assert "Recover only the already-authorized durable scheduler operation" in (
        recovery_prompt
    )
    assert "Do not start a new paid turn" in recovery_prompt
    state["disposition"] = "review_drive_required"
    state_path.write_text(canonical(state), encoding="utf-8")
    print(canonical({"run_id": run_id, "disposition": "review_drive_required"}))
    raise SystemExit(0)

# Simulate the runtime's final pre-turn CAS after wall time crosses a prepared
# authorization boundary. The adapter invocation is recorded, but no root or
# reviewer process (run_count) is started under the stale authorization.
if (
    state["disposition"] == "continue_active_cycle"
    and os.environ.get("MOCK_ACTIVE_AUTH_EXPIRED_AT_REVIEW_DUE")
):
    state["disposition"] = "review_turn_authorization_required"
    state_path.write_text(canonical(state), encoding="utf-8")
    raise SystemExit(70)
if (
    state["disposition"] == "continue_review_only"
    and os.environ.get("MOCK_REVIEW_AUTH_EXPIRED_AT_DEADLINE")
):
    state["disposition"] = "operational_blocked"
    state_path.write_text(canonical(state), encoding="utf-8")
    raise SystemExit(70)

if os.environ.get("MOCK_HOTJOIN_LEGAL_YIELD"):
    snapshot_mcp = mcp_module_paths["mcp.server"].resolve().parent
    sys.path.insert(0, str(snapshot_mcp))
    import server as trusted_generation_server

    problem_id = os.environ["RETHLAS_EXPECTED_PROBLEM_ID"]
    yield_state = os.environ["MOCK_HOTJOIN_LEGAL_YIELD"]
    if yield_state == "1":
        yield_state = "waiting_cost_gate"
    assert yield_state in {"waiting_cost_gate", "waiting_owner_advisor_decision"}
    event_payload = (
        {
            "event_type": "recursive_proving_round",
            "status": yield_state,
            "orchestration_cost": {
                "cost_gate_policy": COST_POLICY_MANIFEST["policy"],
                "cost_gate_policy_manifest_sha256": COST_POLICY_MANIFEST_SHA256,
            },
        }
        if yield_state == "waiting_cost_gate"
        else {
            "event_type": "advisor_checkpoint",
            "status": yield_state,
            "owner_action_required": True,
            "browser_dispatch_authorized": False,
            "advisor_request_id": None,
        }
    )
    checkpoint = trusted_generation_server.memory_append_batch(
        problem_id,
        [
            {"channel": "events", "record": event_payload},
            {
                "channel": "branch_states",
                "record": {
                    "branch_id": "route_mock_owner_yield",
                    "state": {
                        "schema_version": "rethlas_active_route_commitment_v1",
                        "route_id": "route_mock_owner_yield",
                        "status": "active",
                        "core_bridge": (
                            "Preserve the exact unfinished frontier for owner action."
                        ),
                        "obligations": [
                            "Preserve the evidence-bound unfinished route."
                        ],
                    },
                },
            },
            {
                "channel": "branch_states",
                "record": {
                    "branch_id": "mock-cadence-branch",
                    "state": {"status": yield_state},
                },
            },
        ],
    )
    event, active_route, branch = checkpoint["records"]
    state["pending_yield_state"] = yield_state
    state["owner_yield_advisor_record_id"] = (
        event["record_id"]
        if yield_state == "waiting_owner_advisor_decision"
        else None
    )
    latest_publications = json.loads(
        state_path.read_text(encoding="utf-8")
    ).get("memory_batch_publications", {})
    state["memory_batch_publications"] = latest_publications
    state_path.write_text(canonical(state), encoding="utf-8")
    trusted_generation_server.context_handoff_prepare(
        purpose="owner_yield",
        active_route={
            "route_id": "route_mock_owner_yield",
            "core_bridge": "Preserve the exact unfinished frontier for owner action.",
        },
        # Control/advisor events are deliberately excluded from the
        # mathematical handoff frontier.  The host derives any pending owner
        # checkpoint separately from durable control memory.
        # The active-route commitment is host control input, not mathematical
        # frontier evidence, so the handoff cites only the separate durable
        # branch evidence record.
        new_record_ids=[branch["record_id"]],
        obligations=["Do not restart mathematical work before explicit owner action."],
        next_action={
            "description": "Wait for the repository owner to resume the run.",
            "test": "A fresh wrapper records the authenticated owner resume.",
        },
    )
    trusted_generation_server.generation_yield(
        problem_id,
        yield_state,
        "mock cadence legal yield",
        [event["record_id"], branch["record_id"]],
    )

sequence = json.loads(os.environ.get(
    "MOCK_CADENCE_DISPOSITIONS", '["hard_stopped_unfinalized"]'
))
run_count = int(state["run_count"]) + 1
next_disposition = sequence[min(run_count - 1, len(sequence) - 1)]
if os.environ.get("MOCK_POST_TURN_ROUTE_FROZEN"):
    next_disposition = "route_frozen"
    state["allowed_action"] = "freeze_route"
if os.environ.get("MOCK_POST_TURN_STOP_UNSOLVED"):
    next_disposition = "stop_unsolved"
    state["allowed_action"] = "recovery_only"
owner_yield_handoff = state.get("owner_yield_handoff")
handoff_sha256 = (
    owner_yield_handoff["content_sha256"]
    if owner_yield_handoff is not None
    else hashlib.sha256(f"handoff-{run_count}".encode()).hexdigest()
)
prior_epoch = state.get("thread_epoch")
active_epoch_number = (
    prior_epoch["thread_epoch"]
    if isinstance(prior_epoch, dict)
    and prior_epoch.get("state") in {"pending", "active"}
    else max(run_count, 1)
)
pending_handoff = (
    next_disposition == "continue_next_cycle"
    or bool(os.environ.get("MOCK_HOTJOIN_LEGAL_YIELD"))
)
if os.environ.get("MOCK_HOTJOIN_LEGAL_YIELD"):
    # generation_yield has committed its wait record and authenticated handoff,
    # but only owner cadence-close may turn that into a resumable owner_wait.
    next_disposition = "owner_yield_close_required"
if next_disposition == "continue_active_cycle":
    state["prior_allowed_action"] = state.get(
        "allowed_action",
        os.environ.get("MOCK_CADENCE_ALLOWED_ACTION", "free_construction"),
    )
    state["allowed_action"] = "continue_active_cycle_authorized"
state.update({
    "disposition": next_disposition,
    "run_count": run_count,
    "thread_epoch": (
        {
            "active_turn_id": None,
            "handoff_id": f"handoff_{handoff_sha256}",
            "handoff_sha256": handoff_sha256,
            "predecessor_epoch": run_count,
            "state": "pending",
            "thread_epoch": run_count + 1,
            "thread_id": None,
        }
        if pending_handoff
        else {
            "active_turn_id": None,
            "handoff_id": (
                prior_epoch.get("handoff_id")
                if isinstance(prior_epoch, dict)
                else None
            ),
            "handoff_sha256": (
                prior_epoch.get("handoff_sha256")
                if isinstance(prior_epoch, dict)
                else None
            ),
            "predecessor_epoch": (
                active_epoch_number - 1 if active_epoch_number > 1 else None
            ),
            "state": "active",
            "thread_epoch": active_epoch_number,
            "thread_id": f"thread_mock_{active_epoch_number}",
        }
    ),
})
state_path.write_text(canonical(state), encoding="utf-8")
if os.environ.get("MOCK_FAIL_AFTER_YIELD_BEFORE_CADENCE_CLOSE"):
    assert next_disposition == "owner_yield_close_required"
    raise SystemExit(75)
if os.environ.get("MOCK_MUTATE_HOTJOIN_SOURCE"):
    with pathlib.Path(__file__).open("a", encoding="utf-8") as handle:
        handle.write("\n# mutated during scheduler operation\n")
print(canonical({"run_id": run_id, "disposition": next_disposition}))
"""
    adapter_source = adapter_source.replace(
        "__APPROVED_GUARDIAN_LAUNCHER_SHA256__",
        hashlib.sha256(
            (tmp_path / "agents" / "generation" / "guardian_launcher.py").read_bytes()
        ).hexdigest(),
    ).replace(
        "__APPROVED_GUARDIAN_SHA256__",
        hashlib.sha256(
            (tmp_path / "agents" / "generation" / "guardian.py").read_bytes()
        ).hexdigest(),
    ).replace(
        "__APPROVED_GUARDIAN_RUNNER_SHA256__",
        hashlib.sha256(
            (
                tmp_path
                / "agents"
                / "generation"
                / "tests"
                / "run_hotjoin.sh"
            ).read_bytes()
        ).hexdigest(),
    ).replace(
        "__GUARDIAN_CONTROL_SCHEMA_SHA256__",
        GUARDIAN_CONTROL_SCHEMA_SHA256,
    ).replace(
        "__GUARDIAN_LAUNCH_MANIFEST_SCHEMA_SHA256__",
        LAUNCH_MANIFEST_SCHEMA_SHA256,
    )
    adapter_path.write_text(
        adapter_source,
        encoding="utf-8",
    )
    return adapter_path, state_path, calls_path


def _cadence_environment(
    runner: Path,
    fake_bin: Path,
    state_path: Path,
    calls_path: Path,
    *,
    dispositions: list[str],
    max_iterations: int = 2,
    extra_environment: dict[str, str] | None = None,
) -> dict[str, str]:
    fake_curl = fake_bin / "curl"
    fake_curl.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_curl.chmod(0o755)
    environment = _mock_environment(
        runner,
        fake_bin,
        mode="forged",
        extra_environment={
            "MAX_ITERATIONS": str(max_iterations),
            "MOCK_CADENCE_CALLS_FILE": str(calls_path),
            "MOCK_CADENCE_DISPOSITIONS": json.dumps(dispositions),
            "MOCK_CADENCE_EXPECTED_DB": str(
                runner.parents[2] / ".rethlas_hotjoin" / "messages.sqlite3"
            ),
            "MOCK_CADENCE_STATE_FILE": str(state_path),
            "MOCK_GUARDIAN_CONTROL_SCHEMA_SHA256": GUARDIAN_CONTROL_SCHEMA_SHA256,
            "MOCK_GUARDIAN_LAUNCHER_CALLS_FILE": str(
                calls_path.with_name("guardian-launcher-calls.jsonl")
            ),
            "MOCK_GUARDIAN_LAUNCH_MANIFEST_SCHEMA_SHA256": (
                LAUNCH_MANIFEST_SCHEMA_SHA256
            ),
            "RETHLAS_HOTJOIN_RUN_ID": "mock-cadence-live",
            "RETHLAS_REVIEW_CADENCE_POLICY": "rethlas_route_review_150m_v2",
            **(extra_environment or {}),
        },
    )
    selected = environment.get("RETHLAS_COST_GATE_POLICY", "owner_gated")
    manifest = json.dumps(
        {
            "schema_version": "rethlas_resolved_cost_policy_v1",
            "policy": selected,
            "authority": "owner_wrapper",
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    environment["RETHLAS_RESOLVED_COST_POLICY_JSON"] = manifest
    environment["RETHLAS_RESOLVED_COST_POLICY_SHA256"] = hashlib.sha256(
        manifest.encode()
    ).hexdigest()
    return environment


def _cadence_calls(calls_path: Path, command: str) -> list[dict[str, object]]:
    return [
        value
        for value in map(
            json.loads, calls_path.read_text(encoding="utf-8").splitlines()
        )
        if value["command"] == command
    ]


def _assert_cadence_capabilities_are_fd_only(
    calls_path: Path,
    state_path: Path,
) -> None:
    calls = [
        json.loads(line)
        for line in calls_path.read_text(encoding="utf-8").splitlines()
    ]
    state = json.loads(state_path.read_text(encoding="utf-8"))
    owner_digests = set(state["token_digests"])
    assert state["capability_revision"] == len(state["token_digests"])
    assert state["capability_revision"] >= 1
    owner_commands = {
        "control-capability-bind",
        "cadence-admit",
        "cadence-close",
        "review-drive",
    }
    owner_calls = [call for call in calls if call["command"] in owner_commands]
    assert owner_calls
    for call in owner_calls:
        arguments = call["argv"]
        capability = call["control_capability"]
        assert isinstance(arguments, list)
        assert isinstance(capability, dict)
        assert "--control-token-fd" in arguments
        assert arguments[arguments.index("--control-token-domain") + 1] == "owner"
        assert capability["domain"] == "owner"
        assert capability["sha256"] in owner_digests
        assert call["runner_capability_sha256"] is None
        assert call["capability_env_present"] is False

    owner_manifest_calls = [
        call
        for call in calls
        if call["command"] == "review-status"
        and isinstance(call.get("control_envelope"), dict)
        and call["control_envelope"].get("payload", {}).get("operation")
        == "memory_batch_publication_status"
        and call["control_capability"] is not None
    ]
    assert owner_manifest_calls
    for call in owner_manifest_calls:
        arguments = call["argv"]
        capability = call["control_capability"]
        assert "--control-token-fd" in arguments
        assert arguments[arguments.index("--control-token-domain") + 1] == "owner"
        assert capability["domain"] == "owner"
        assert capability["sha256"] in owner_digests
        assert call["runner_capability_sha256"] is None
        assert call["capability_env_present"] is False

    runner_calls = [call for call in calls if call["command"] == "run-generator"]
    assert runner_calls
    for call in runner_calls:
        arguments = call["argv"]
        runner_digest = call["runner_capability_sha256"]
        assert isinstance(arguments, list)
        assert "--runner-token-fd" in arguments
        assert call["control_capability"] is None
        assert isinstance(runner_digest, str) and len(runner_digest) == 64
        assert runner_digest not in owner_digests
        assert call["capability_env_present"] is False

    launcher_calls_path = calls_path.with_name("guardian-launcher-calls.jsonl")
    launcher_calls = [
        json.loads(line)
        for line in launcher_calls_path.read_text(encoding="utf-8").splitlines()
    ]
    assert len(launcher_calls) == len(runner_calls)
    for launch, run_call in zip(launcher_calls, runner_calls, strict=True):
        arguments = launch["argv"]
        worker_command = launch["worker_command"]
        assert isinstance(arguments, list)
        assert isinstance(worker_command, list)
        assert "--owner-token-fd" in arguments
        assert "--runner-token-fd" not in worker_command
        assert "--control-token-fd" not in worker_command
        assert launch["owner_token_sha256"] in owner_digests
        assert launch["runner_token_sha256"] == run_call[
            "runner_capability_sha256"
        ]
        assert launch["capability_env_present"] is False


def _assert_guarded_review_drive_is_fd_only(
    calls_path: Path,
    state_path: Path,
    *,
    expected_count: int = 1,
) -> None:
    drives = _cadence_calls(calls_path, "guarded-review-drive")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    owner_digests = set(state["token_digests"])
    assert len(drives) == expected_count
    assert not _cadence_calls(calls_path, "review-drive")
    for drive in drives:
        arguments = drive["argv"]
        runner_digest = drive["runner_capability_sha256"]
        assert isinstance(arguments, list)
        assert arguments.count("--runner-token-fd") == 1
        assert "--control-token-fd" not in arguments
        assert drive["control_capability"] is None
        assert drive["control_envelope"] is None
        assert drive["capability_env_present"] is False
        assert isinstance(runner_digest, str) and len(runner_digest) == 64
        assert runner_digest not in owner_digests

    launcher_calls_path = calls_path.with_name("guardian-launcher-calls.jsonl")
    launcher_calls = [
        json.loads(line)
        for line in launcher_calls_path.read_text(encoding="utf-8").splitlines()
    ]
    guarded_launches = [
        launch
        for launch in launcher_calls
        if "guarded-review-drive" in launch["worker_command"]
    ]
    assert len(guarded_launches) == expected_count
    for launch, drive in zip(guarded_launches, drives, strict=True):
        worker_command = launch["worker_command"]
        assert launch["admission_mode"] == "same_cycle_resume"
        assert launch["owner_token_sha256"] in owner_digests
        assert launch["runner_token_sha256"] == drive[
            "runner_capability_sha256"
        ]
        assert launch["capability_env_present"] is False
        assert "--runner-token-fd" not in worker_command
        assert "--control-token-fd" not in worker_command
        guarded_index = worker_command.index("guarded-review-drive")
        assert worker_command[guarded_index + 1 :] == [
            "--run-id",
            "mock-cadence-live",
            "--boundary-id",
            "reviewbound_" + "b" * 32,
        ]


def _seed_mock_cadence_projection(
    adapter_path: Path,
    state_path: Path,
    environment: dict[str, str],
    *,
    disposition: str,
) -> None:
    subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            str(adapter_path),
            "--db",
            environment["MOCK_CADENCE_EXPECTED_DB"],
            "init",
            "--run-id",
            environment["RETHLAS_HOTJOIN_RUN_ID"],
            "--problem-id",
            "example",
        ],
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state.update(
        {
            "cycle_history": ["cycle_" + f"{1:032x}"],
            "cycle_id": "cycle_" + f"{1:032x}",
            "cycle_serial": 1,
            "disposition": disposition,
            "generation": 1,
            "guardian_clock_sha256": hashlib.sha256(
                b"guardian-clock-1"
            ).hexdigest(),
            "run_count": 1,
            "thread_epoch": {
                "active_turn_id": None,
                "handoff_id": None,
                "handoff_sha256": None,
                "predecessor_epoch": None,
                "state": "active",
                "thread_epoch": 1,
                "thread_id": "thread_mock_1",
            },
        }
    )
    if disposition in {
        "post_review_handoff_required",
        "continue_reviewed_cycle_fresh_epoch",
    }:
        state["allowed_action"] = "post_review_handoff_required"
    elif disposition == "route_frozen":
        state["allowed_action"] = "recovery_only"
    state_path.write_text(
        json.dumps(state, allow_nan=False, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )


def _make_nonfresh_ledger_copy(
    runner: Path,
    tmp_path: Path,
) -> tuple[Path, Path]:
    source = runner.parents[2] / ".rethlas_hotjoin" / "messages.sqlite3"
    source.parent.mkdir(mode=0o700)
    with sqlite3.connect(source) as connection:
        connection.execute(
            "CREATE TABLE legacy_probe (run_id TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO legacy_probe(run_id, value) VALUES (?, ?)",
            ("mock-cadence-live", "copied legacy ledger"),
        )
    source.chmod(0o600)
    copy = (tmp_path / "nonfresh-copy" / "messages.copy.sqlite3").resolve()
    copy.parent.mkdir(mode=0o700)
    shutil.copy2(source, copy)
    copy.chmod(0o600)
    assert source.read_bytes() == copy.read_bytes()
    assert source.stat().st_ino != copy.stat().st_ino
    return source, copy


def test_cadence_policy_without_hotjoin_starts_zero_codex_processes(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    codex_calls = tmp_path / "codex-calls.jsonl"
    environment = _mock_environment(
        runner,
        fake_bin,
        mode="forged",
        extra_environment={
            "MOCK_CODEX_CALLS_FILE": str(codex_calls),
            "RETHLAS_REVIEW_CADENCE_POLICY": "rethlas_route_review_150m_v2",
            "RETHLAS_CONTEXT_GUARD_POLICY": "rethlas_context_guard_v1",
        },
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )

    assert completed.returncode == 1
    assert "require RETHLAS_HOTJOIN_RUN_ID" in completed.stderr
    assert not codex_calls.exists()


def test_mode_prompt_explains_tradeoffs_and_selects_core(tmp_path: Path) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    environment = _mock_environment(
        runner,
        fake_bin,
        mode="trusted",
        extra_environment={"RETHLAS_RUN_MODE": "prompt"},
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        input="1\n1\n",
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "Choose an AxiomRelay execution mode" in completed.stderr
    assert "Lower model-token and operational overhead" in completed.stderr
    assert "No T+60/T+120 route reviews" in completed.stderr
    assert "no trusted frontier delta means no next paid" in completed.stderr
    assert "Uses more model work" in completed.stderr
    assert "Choose the route-design main agent" in completed.stderr
    assert "Persistent logical Claude Code root" in completed.stderr
    assert "Mode:       core" in completed.stdout
    assert "Main agent: gpt-sol" in completed.stdout


def test_noninteractive_launcher_requires_an_explicit_problem_file(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    environment = _mock_environment(runner, fake_bin, mode="trusted")
    environment.pop("PROBLEM_FILE")

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )

    assert completed.returncode == 1
    assert "PROBLEM_FILE is required in noninteractive use" in completed.stderr


def test_core_prefers_documented_generation_venv_over_path_python(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    shadow_bin = tmp_path / "path-shadow"
    shadow_bin.mkdir()
    (shadow_bin / "python3").symlink_to(fake_bin / "python3")
    fake_codex = fake_bin / "codex"
    fake_codex.write_text(
        fake_codex.read_text(encoding="utf-8").replace(
            "#!/usr/bin/env python3",
            f"#!{fake_bin / 'python'}",
            1,
        ),
        encoding="utf-8",
    )
    environment = _mock_environment(runner, fake_bin, mode="trusted")
    environment["PATH"] = (
        f"{shadow_bin}{os.pathsep}{fake_bin}{os.pathsep}{os.environ['PATH']}"
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "Generation python3 must be a non-symlink executable" not in completed.stderr


def test_reviewed_prefers_documented_generation_venv_over_path_python(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    shadow_bin = tmp_path / "reviewed-path-shadow"
    shadow_bin.mkdir()
    (shadow_bin / "python3").symlink_to(fake_bin / "python3")
    shutil.rmtree(_module_stub(fake_bin, "sympy"))
    environment = _mock_environment(
        runner,
        fake_bin,
        mode="forged",
        extra_environment={
            "AXIOM_RELAY_RUN_MODE": "reviewed",
            "AXIOM_RELAY_REVIEW_RUN_ID": "reviewed-default-python",
        },
    )
    environment["PATH"] = (
        f"{shadow_bin}{os.pathsep}{fake_bin}{os.pathsep}{os.environ['PATH']}"
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 1
    assert "sympy: module not found" in completed.stderr
    assert "non-symlink Python interpreter" not in completed.stderr


def test_claude_root_accepts_official_native_two_hardlink_install(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    environment = _mock_environment(
        runner,
        fake_bin,
        mode="trusted",
        extra_environment={
            "RETHLAS_MAIN_AGENT": "opus",
            "RETHLAS_CLAUDE_ROOT_PRINT_CMD": "1",
        },
    )
    current, versioned = _install_mock_official_native_claude(
        Path(environment["HOME"]),
        fake_bin / "claude",
    )
    environment["RETHLAS_CLAUDE_BIN"] = str(current)

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert versioned.stat().st_nlink == 2
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "Claude CLI failed its trust check" not in completed.stderr


def test_claude_root_rejects_arbitrary_second_hardlink(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    os.link(fake_bin / "claude", tmp_path / "untrusted-claude-alias")
    environment = _mock_environment(
        runner,
        fake_bin,
        mode="trusted",
        extra_environment={
            "RETHLAS_MAIN_AGENT": "opus",
            "RETHLAS_CLAUDE_ROOT_PRINT_CMD": "1",
        },
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 70
    assert "Claude CLI failed its trust check" in completed.stderr


def test_claude_runtime_accepts_canonical_tmpdir_parent_alias(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    canonical_tmp = tmp_path / "canonical-tmp"
    canonical_tmp.mkdir()
    tmp_alias = tmp_path / "tmp-alias"
    tmp_alias.symlink_to(canonical_tmp, target_is_directory=True)
    environment = _mock_environment(
        runner,
        fake_bin,
        mode="trusted",
        extra_environment={
            "RETHLAS_MAIN_AGENT": "opus",
            "RETHLAS_CLAUDE_ROOT_PRINT_CMD": "1",
            "TMPDIR": str(tmp_alias),
        },
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "runtime dependency bundle root is unsafe" not in completed.stderr


@pytest.mark.parametrize(
    ("selection", "agent", "canonical", "cli_model"),
    [
        ("2", "opus", "claude-opus-5", "claude-opus-5[1m]"),
        ("3", "fable", "claude-fable-5", "claude-fable-5"),
    ],
)
def test_prompted_core_selects_persistent_claude_root_without_sol_root(
    tmp_path: Path,
    selection: str,
    agent: str,
    canonical: str,
    cli_model: str,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    calls_file = tmp_path / f"{agent}-codex-calls.jsonl"
    environment = _mock_environment(
        runner,
        fake_bin,
        mode="trusted",
        extra_environment={
            "RETHLAS_RUN_MODE": "prompt",
            "RETHLAS_CLAUDE_ROOT_PRINT_CMD": "1",
            "MOCK_CODEX_CALLS_FILE": str(calls_file),
        },
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        input=f"1\n{selection}\n",
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert f"Claude root model: {canonical}" in completed.stdout
    assert f"Claude root CLI model: {cli_model}" in completed.stdout
    assert cli_model.replace("[", r"\[").replace("]", r"\]") in completed.stdout
    assert (
        "Claude root response-segment output tokens: 48000 "
        "(cumulative unbounded)" in completed.stdout
    )
    assert "--effort max" in completed.stdout
    assert "--permission-mode dontAsk" in completed.stdout
    assert "bypassPermissions" not in completed.stdout
    assert "--allowedTools" in completed.stdout
    assert "mcp__rethlas-root__search_matlas_theorems" in completed.stdout
    assert "mcp__rethlas-root__search_arxiv_theorems" in completed.stdout
    assert "mcp__rethlas-root__read_arxiv_primary" in completed.stdout
    assert "mcp__rethlas-root__run_three_route_cohort" in completed.stdout
    assert "mcp__rethlas-root__start_route_council" not in completed.stdout
    assert "mcp__rethlas-root__edit_blueprint" in completed.stdout
    assert "enabledMcpjsonServers" in completed.stdout
    assert "rethlas-root" in completed.stdout
    assert "--strict-mcp-config" in completed.stdout
    assert "--tools Read" in completed.stdout
    assert f"--add-dir {runner.parents[1] / 'data'}" in completed.stdout
    assert f"--add-dir {runner.parents[1] / 'results'}" in completed.stdout
    assert (runner.parents[1] / "results").is_dir()
    assert "Claude retrieval mode: disabled" in completed.stdout
    assert "Write" not in completed.stdout
    assert "Edit" not in completed.stdout
    assert not calls_file.exists()
    assert not (runner.parents[1].parent / ".claude_core").exists()


def test_prompted_core_selects_opus_sol_council_with_one_joint_revision(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    calls_file = tmp_path / "council-codex-calls.jsonl"
    environment = _mock_environment(
        runner,
        fake_bin,
        mode="trusted",
        extra_environment={
            "RETHLAS_RUN_MODE": "prompt",
            "RETHLAS_CLAUDE_ROOT_PRINT_CMD": "1",
            "MOCK_CODEX_CALLS_FILE": str(calls_file),
        },
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        input="1\n4\n",
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "Opus + Sol council" in completed.stderr
    assert "Claude root model: claude-opus-5" in completed.stdout
    assert "Claude orchestration mode: opus_sol_council_v2" in completed.stdout
    for tool in (
        "route_council_status",
        "start_route_council",
        "revise_route_council",
        "finalize_route_council",
        "override_route_council",
    ):
        assert f"mcp__rethlas-root__{tool}" in completed.stdout
    assert "one\\ joint\\ revision" in completed.stdout
    assert "never\\ a\\ third\\ edit\\ dialogue" in completed.stdout
    assert not calls_file.exists()


def test_claude_root_launcher_never_relabels_loaded_source_after_atomic_replacement(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    core_source = runner.parents[1].parent / "claude_core.py"
    loaded_marker = tmp_path / "loaded-source.marker"
    release_marker = tmp_path / "release-source.marker"
    replacement_executed = tmp_path / "replacement-executed.marker"
    original = core_source.read_text(encoding="utf-8")
    future = "from __future__ import annotations\n"
    barrier = (
        "import os as _source_barrier_os\n"
        "import pathlib as _source_barrier_pathlib\n"
        "import time as _source_barrier_time\n"
        "_source_loaded = _source_barrier_os.environ.get('SOURCE_LOADED_MARKER')\n"
        "_source_release = _source_barrier_os.environ.get('SOURCE_RELEASE_MARKER')\n"
        "if _source_loaded and _source_release:\n"
        "    _source_loaded_path = _source_barrier_pathlib.Path(_source_loaded)\n"
        "    _source_release_path = _source_barrier_pathlib.Path(_source_release)\n"
        "    if not _source_loaded_path.exists():\n"
        "        _source_loaded_path.write_text('loaded', encoding='utf-8')\n"
        "        while not _source_release_path.exists():\n"
        "            _source_barrier_time.sleep(0.01)\n"
    )
    source_a = original.replace(future, future + barrier, 1)
    replacement_probe = (
        "import os as _replacement_os\n"
        "import pathlib as _replacement_pathlib\n"
        "_replacement_marker = _replacement_os.environ.get("
        "'REPLACEMENT_EXECUTED_MARKER')\n"
        "if _replacement_marker:\n"
        "    _replacement_pathlib.Path(_replacement_marker).write_text("
        "'executed', encoding='utf-8')\n"
    )
    source_b = original.replace(future, future + replacement_probe, 1)
    assert hashlib.sha256(source_a.encode()).hexdigest() != hashlib.sha256(
        source_b.encode()
    ).hexdigest()
    core_source.write_text(source_a, encoding="utf-8")
    replacement = tmp_path / "replacement-claude-core.py"
    replacement.write_text(source_b, encoding="utf-8")
    verifier_profile = {
        "schema_version": "rethlas_verifier_profile_v1",
        "profile": "max_diversity",
        "fallback_policy": "forbid",
        "automatic_tiebreaker": False,
        "passes": [
            {
                "model": "gpt-5.6-sol",
                "adapter": "codex_cli",
                "provider": "openai",
            },
            {
                "model": "claude-opus-5",
                "adapter": "claude_cli",
                "provider": "anthropic",
            },
        ],
    }
    fake_curl = fake_bin / "curl"
    fake_curl.write_text(
        "#!/bin/sh\n"
        "case \"$*\" in\n"
        f"  */profile*) printf '%s\\n' '{json.dumps(verifier_profile)}' ;;\n"
        "  *) printf '%s\\n' '{}' ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    fake_curl.chmod(0o755)
    environment = _mock_environment(
        runner,
        fake_bin,
        mode="trusted",
        extra_environment={
            "RETHLAS_RUN_MODE": "core",
            "RETHLAS_MAIN_AGENT": "opus-sol-council",
            "SOURCE_LOADED_MARKER": str(loaded_marker),
            "SOURCE_RELEASE_MARKER": str(release_marker),
            "REPLACEMENT_EXECUTED_MARKER": str(replacement_executed),
        },
    )
    process = subprocess.Popen(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 10
        while not loaded_marker.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert loaded_marker.exists(), "authenticated source never reached its barrier"
        os.replace(replacement, core_source)
        release_marker.write_text("continue", encoding="utf-8")
        stdout, stderr = process.communicate(timeout=30)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)

    assert process.returncode == 70, stdout + stderr
    assert "Claude Core changed during dependency closure snapshot" in stderr
    assert "runtime dependency closure" in stderr
    assert not replacement_executed.exists()
    state_root = runner.parents[1].parent / ".claude_core"
    if state_root.exists():
        assert not list(state_root.rglob("manifest.json"))


def test_inflight_root_launcher_pins_loaded_inode_across_atomic_replacement(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    launcher = runner.with_name("run_claude_core.sh")
    entered = tmp_path / "launcher-a-entered.marker"
    release = tmp_path / "launcher-a-release.marker"
    original = launcher.read_text(encoding="utf-8")
    needle = 'root_launcher_image_path="$descriptor_root/${root_launcher_image_fd}"\n'
    barrier = (
        'if [[ -n "${LAUNCHER_A_ENTERED:-}" && -n '
        '"${LAUNCHER_A_RELEASE:-}" ]]; then\n'
        '  : > "$LAUNCHER_A_ENTERED"\n'
        '  while [[ ! -e "$LAUNCHER_A_RELEASE" ]]; do sleep 0.01; done\n'
        "fi\n"
    )
    assert needle in original
    source_a = original.replace(needle, needle + barrier, 1)
    source_b = original + "\n# atomic deployment B\n"
    launcher.write_text(source_a, encoding="utf-8")
    launcher.chmod(0o755)
    replacement = tmp_path / "run_claude_core.deployment-b.sh"
    replacement.write_text(source_b, encoding="utf-8")
    replacement.chmod(0o755)
    digest_a = hashlib.sha256(source_a.encode("utf-8")).hexdigest()
    digest_b = hashlib.sha256(source_b.encode("utf-8")).hexdigest()
    assert digest_a != digest_b
    fake_curl = fake_bin / "curl"
    fake_curl.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_curl.chmod(0o755)
    environment = _mock_environment(
        runner,
        fake_bin,
        mode="trusted",
        extra_environment={
            "RETHLAS_RUN_MODE": "core",
            "RETHLAS_MAIN_AGENT": "opus",
            "LAUNCHER_A_ENTERED": str(entered),
            "LAUNCHER_A_RELEASE": str(release),
        },
    )
    process = subprocess.Popen(
        [str(launcher)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 10
        while not entered.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert entered.exists(), "launcher A did not duplicate its loaded fd"
        os.replace(replacement, launcher)
        release.write_text("continue", encoding="utf-8")
        stdout_a, stderr_a = process.communicate(timeout=30)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)
    assert process.returncode == 0, stdout_a + stderr_a
    assert f"Claude root launcher SHA-256: {digest_a}" in stdout_a
    assert digest_b not in stdout_a

    environment.pop("LAUNCHER_A_ENTERED")
    environment.pop("LAUNCHER_A_RELEASE")
    active_root_path = (
        runner.parents[1].parent
        / ".claude_core"
        / "example"
        / "active_root.json"
    )
    active_root = json.loads(active_root_path.read_text(encoding="utf-8"))
    environment["RETHLAS_CLAUDE_ROOT_SESSION_ID"] = active_root[
        "root_session_id"
    ]
    completed_b = subprocess.run(
        [str(launcher)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert completed_b.returncode != 0
    assert (
        "active root launcher execution epoch differs from this deployment"
        in completed_b.stderr
    )
    assert "incomplete provider binding" not in completed_b.stderr
    assert "--resume" not in completed_b.stdout


def test_claude_root_rejects_mixed_core_and_dependency_deployment(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    core_source = runner.parents[1].parent / "claude_core.py"
    dependency = runner.parents[1] / "mcp" / "publication_proof_context_v3.py"
    entered = tmp_path / "dependency-closure-entered.marker"
    release = tmp_path / "dependency-closure-release.marker"
    original_core = core_source.read_text(encoding="utf-8")
    future = "from __future__ import annotations\n"
    barrier = (
        "import os as _closure_barrier_os\n"
        "import pathlib as _closure_barrier_pathlib\n"
        "import time as _closure_barrier_time\n"
        "_closure_entered = _closure_barrier_os.environ.get("
        "'DEPENDENCY_CLOSURE_ENTERED')\n"
        "_closure_release = _closure_barrier_os.environ.get("
        "'DEPENDENCY_CLOSURE_RELEASE')\n"
        "if _closure_entered and _closure_release:\n"
        "    _closure_entered_path = _closure_barrier_pathlib.Path("
        "_closure_entered)\n"
        "    _closure_release_path = _closure_barrier_pathlib.Path("
        "_closure_release)\n"
        "    if not _closure_entered_path.exists():\n"
        "        _closure_entered_path.write_text('entered', encoding='utf-8')\n"
        "        while not _closure_release_path.exists():\n"
        "            _closure_barrier_time.sleep(0.01)\n"
    )
    core_source.write_text(
        original_core.replace(future, future + barrier, 1),
        encoding="utf-8",
    )
    replacement = tmp_path / "publication-proof-context-v3-b.py"
    replacement.write_text(
        dependency.read_text(encoding="utf-8") + "\n# deployment B\n",
        encoding="utf-8",
    )
    calls_file = tmp_path / "dependency-closure-claude-calls.jsonl"
    environment = _mock_environment(
        runner,
        fake_bin,
        mode="trusted",
        extra_environment={
            "RETHLAS_RUN_MODE": "core",
            "RETHLAS_MAIN_AGENT": "opus",
            "DEPENDENCY_CLOSURE_ENTERED": str(entered),
            "DEPENDENCY_CLOSURE_RELEASE": str(release),
            "MOCK_CLAUDE_CALLS_FILE": str(calls_file),
        },
    )
    process = subprocess.Popen(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 10
        while not entered.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert entered.exists(), "Core never reached dependency snapshot barrier"
        os.replace(replacement, dependency)
        release.write_text("continue", encoding="utf-8")
        stdout, stderr = process.communicate(timeout=30)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)

    assert process.returncode == 70, stdout + stderr
    assert "differs from Core binding" in stderr
    assert "publication_proof_context_v3.py" in stderr
    assert not calls_file.exists()
    state_root = runner.parents[1].parent / ".claude_core"
    if state_root.exists():
        assert not list(state_root.rglob("manifest.json"))


def test_opus_sol_council_rejects_explicit_non_diversity_profile_before_models(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    calls_file = tmp_path / "council-profile-codex-calls.jsonl"
    environment = _mock_environment(
        runner,
        fake_bin,
        mode="trusted",
        extra_environment={
            "RETHLAS_RUN_MODE": "core",
            "RETHLAS_MAIN_AGENT": "opus-sol-council",
            "RETHLAS_MODEL_POLICY_PROFILE": "compatible",
            "MOCK_CODEX_CALLS_FILE": str(calls_file),
        },
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )

    assert completed.returncode == 1
    assert (
        "requires AXIOM_RELAY_MODEL_POLICY_PROFILE=max_diversity"
        in completed.stderr
    )
    assert not calls_file.exists()


def test_claude_root_prompt_announces_explicit_matlas_arxiv_permission(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    problem = runner.parents[1] / "data" / "example.md"
    problem.write_text(
        "S\n\n## Retrieval restriction\n\n"
        "Matlas and arXiv retrieval are permitted. Use no arXiv source whose "
        "initial submission date is later than 2026-06-26.\n",
        encoding="utf-8",
    )
    environment = _mock_environment(
        runner,
        fake_bin,
        mode="trusted",
        extra_environment={
            "RETHLAS_RUN_MODE": "core",
            "RETHLAS_MAIN_AGENT": "opus",
            "RETHLAS_CLAUDE_ROOT_PRINT_CMD": "1",
        },
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "Claude retrieval mode: matlas_arxiv" in completed.stdout
    assert r"statement_bound_retrieval_mode=matlas_arxiv" in completed.stdout
    assert "mcp__rethlas-root__search_matlas_theorems" in completed.stdout
    assert "mcp__rethlas-root__search_arxiv_theorems" in completed.stdout
    assert "mcp__rethlas-root__read_arxiv_primary" in completed.stdout
    assert "--no-chrome" in completed.stdout


@pytest.mark.parametrize(
    "value", ["0", "47999", "48001", "128000", "not-an-integer"]
)
def test_claude_root_rejects_invalid_max_output_tokens_before_launch(
    tmp_path: Path, value: str
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    environment = _mock_environment(
        runner,
        fake_bin,
        mode="trusted",
        extra_environment={
            "RETHLAS_RUN_MODE": "core",
            "RETHLAS_MAIN_AGENT": "fable",
            "RETHLAS_CLAUDE_ROOT_PRINT_CMD": "1",
            "CLAUDE_CODE_MAX_OUTPUT_TOKENS": value,
        },
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )

    assert completed.returncode == 1
    assert "require the liveness-safe CLAUDE_CODE_MAX_OUTPUT_TOKENS=48000" in (
        completed.stderr
    )
    assert "cumulative output unbounded" in completed.stderr
    assert "Claude root session:" not in completed.stdout
    assert not (runner.parents[1].parent / ".claude_core").exists()


def test_claude_root_accepts_explicit_liveness_safe_response_segment(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    environment = _mock_environment(
        runner,
        fake_bin,
        mode="trusted",
        extra_environment={
            "RETHLAS_RUN_MODE": "core",
            "RETHLAS_MAIN_AGENT": "fable",
            "RETHLAS_CLAUDE_ROOT_PRINT_CMD": "1",
            "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "48000",
        },
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert (
        "Claude root response-segment output tokens: 48000 "
        "(cumulative unbounded)" in completed.stdout
    )


@pytest.mark.parametrize(
    ("main_agent", "provider_model_key", "provider_model"),
    [
        ("opus", "ANTHROPIC_DEFAULT_OPUS_MODEL", "claude-opus-test"),
        ("fable", "ANTHROPIC_DEFAULT_FABLE_MODEL", "claude-fable-test"),
    ],
)
def test_claude_root_projects_only_selected_allowlisted_vertex_user_settings(
    tmp_path: Path,
    main_agent: str,
    provider_model_key: str,
    provider_model: str,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    fake_home = tmp_path / "home"
    settings_dir = fake_home / ".claude"
    settings_dir.mkdir(parents=True)
    settings = settings_dir / "settings.json"
    settings.write_text(
        json.dumps(
            {
                "env": {
                    "CLAUDE_CODE_USE_VERTEX": "1",
                    "ANTHROPIC_VERTEX_PROJECT_ID": "private-project-marker",
                    "CLOUD_ML_REGION": "us-east5",
                    provider_model_key: provider_model,
                    (
                        "ANTHROPIC_DEFAULT_FABLE_MODEL"
                        if provider_model_key == "ANTHROPIC_DEFAULT_OPUS_MODEL"
                        else "ANTHROPIC_DEFAULT_OPUS_MODEL"
                    ): "must-not-project-unused-model",
                    "UNTRUSTED_EXTRA_SETTING": "must-not-be-projected",
                },
                "hooks": {"must-not-load": "project-only mode remains binding"},
            }
        ),
        encoding="utf-8",
    )
    settings.chmod(0o600)
    environment = _mock_environment(
        runner,
        fake_bin,
        mode="trusted",
        extra_environment={
            "HOME": str(fake_home),
            "RETHLAS_RUN_MODE": "core",
            "RETHLAS_MAIN_AGENT": main_agent,
            "RETHLAS_CLAUDE_ROOT_PRINT_CMD": "1",
        },
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert (
        "Claude provider projection: vertex-user-settings-allowlist"
        in completed.stdout
    )
    assert "Claude thinking display projection: vertex-summarized" in completed.stdout
    assert "--setting-sources project" in completed.stdout
    assert "private-project-marker" not in completed.stdout + completed.stderr
    assert "must-not-project-unused-model" not in completed.stdout + completed.stderr
    assert "must-not-be-projected" not in completed.stdout + completed.stderr


def test_claude_root_subscription_uses_dedicated_config_directory(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    fake_home = tmp_path / "home"
    home_settings = fake_home / ".claude" / "settings.json"
    home_settings.parent.mkdir(parents=True)
    home_settings.write_text(
        json.dumps(
            {
                "env": {
                    "CLAUDE_CODE_USE_VERTEX": "1",
                    "ANTHROPIC_VERTEX_PROJECT_ID": "must-not-leak",
                    "CLOUD_ML_REGION": "global",
                    "ANTHROPIC_DEFAULT_OPUS_MODEL": "must-not-leak",
                }
            }
        ),
        encoding="utf-8",
    )
    dedicated_config = tmp_path / "subscription-claude"
    dedicated_config.mkdir()
    environment = _mock_environment(
        runner,
        fake_bin,
        mode="trusted",
        extra_environment={
            "HOME": str(fake_home),
            "CLAUDE_CONFIG_DIR": str(dedicated_config),
            "RETHLAS_RUN_MODE": "core",
            "RETHLAS_MAIN_AGENT": "opus",
            "RETHLAS_CLAUDE_ROOT_PRINT_CMD": "1",
            "AXIOM_RELAY_CLAUDE_AUTH_MODE": "subscription",
        },
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "Claude provider projection: cli-default" in completed.stdout
    assert "Claude provider: anthropic" in completed.stdout
    assert "must-not-leak" not in completed.stdout + completed.stderr


def test_claude_root_subscription_ignores_default_vertex_settings(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    fake_home = tmp_path / "home"
    home_settings = fake_home / ".claude" / "settings.json"
    home_settings.parent.mkdir(parents=True)
    home_settings.write_text(
        json.dumps(
            {
                "env": {
                    "CLAUDE_CODE_USE_VERTEX": "1",
                    "ANTHROPIC_VERTEX_PROJECT_ID": "must-not-leak",
                    "CLOUD_ML_REGION": "global",
                    "ANTHROPIC_DEFAULT_OPUS_MODEL": "must-not-leak",
                }
            }
        ),
        encoding="utf-8",
    )
    fake_curl = fake_bin / "curl"
    fake_curl.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_curl.chmod(0o755)
    claude_calls = tmp_path / "claude-calls.jsonl"
    environment = _mock_environment(
        runner,
        fake_bin,
        mode="trusted",
        extra_environment={
            "HOME": str(fake_home),
            "RETHLAS_RUN_MODE": "core",
            "RETHLAS_MAIN_AGENT": "opus",
            "AXIOM_RELAY_CLAUDE_AUTH_MODE": "subscription",
            "MOCK_CLAUDE_AUTH_METHOD": "claude.ai",
            "MOCK_CLAUDE_SUBSCRIPTION_TYPE": "max",
            "MOCK_CLAUDE_CALLS_FILE": str(claude_calls),
        },
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "Claude provider projection: cli-default" in completed.stdout
    assert "Claude provider: anthropic" in completed.stdout
    assert "Claude auth mode/method: subscription/claude.ai" in completed.stdout
    assert "must-not-leak" not in completed.stdout + completed.stderr
    assert len(claude_calls.read_text(encoding="utf-8").splitlines()) == 1


def test_claude_root_subscription_binds_auth_method_before_paid_turn(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    fake_curl = fake_bin / "curl"
    fake_curl.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_curl.chmod(0o755)
    claude_calls = tmp_path / "claude-calls.jsonl"
    environment = _mock_environment(
        runner,
        fake_bin,
        mode="trusted",
        extra_environment={
            "RETHLAS_RUN_MODE": "core",
            "RETHLAS_MAIN_AGENT": "opus",
            "AXIOM_RELAY_CLAUDE_AUTH_MODE": "subscription",
            "MOCK_CLAUDE_AUTH_METHOD": "claude.ai",
            "MOCK_CLAUDE_SUBSCRIPTION_TYPE": "max",
            "MOCK_CLAUDE_CALLS_FILE": str(claude_calls),
        },
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "Claude auth mode/method: subscription/claude.ai" in completed.stdout
    assert len(claude_calls.read_text(encoding="utf-8").splitlines()) == 1


def test_claude_root_subscription_rejects_stored_api_auth_before_paid_turn(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    claude_calls = tmp_path / "claude-calls.jsonl"
    environment = _mock_environment(
        runner,
        fake_bin,
        mode="trusted",
        extra_environment={
            "RETHLAS_RUN_MODE": "core",
            "RETHLAS_MAIN_AGENT": "opus",
            "AXIOM_RELAY_CLAUDE_AUTH_MODE": "subscription",
            "MOCK_CLAUDE_AUTH_METHOD": "api_key",
            "MOCK_CLAUDE_CALLS_FILE": str(claude_calls),
        },
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 1
    assert "bound provider/auth mode" in completed.stderr
    assert not claude_calls.exists()
    assert not (runner.parents[1].parent / ".claude_core").exists()


def test_claude_root_completes_partial_inherited_vertex_binding_from_launch_model(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    fake_home = tmp_path / "home"
    settings_dir = fake_home / ".claude"
    settings_dir.mkdir(parents=True)
    (settings_dir / "settings.json").write_text(
        json.dumps({"env": {"UNRELATED_SETTING": "ignored"}}),
        encoding="utf-8",
    )
    (settings_dir / "settings.json").chmod(0o600)
    environment = _mock_environment(
        runner,
        fake_bin,
        mode="trusted",
        extra_environment={
            "HOME": str(fake_home),
            "RETHLAS_RUN_MODE": "core",
            "RETHLAS_MAIN_AGENT": "opus-sol-council",
            "RETHLAS_CLAUDE_ROOT_PRINT_CMD": "1",
            "CLAUDE_CODE_USE_VERTEX": "1",
            "ANTHROPIC_VERTEX_PROJECT_ID": "bound-project",
            "CLOUD_ML_REGION": "global",
        },
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert (
        "Claude provider projection: "
        "vertex-process-plus-host-model-default"
    ) in completed.stdout


def test_claude_root_rejects_conflicting_partial_vertex_projection(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    fake_home = tmp_path / "home"
    settings_dir = fake_home / ".claude"
    settings_dir.mkdir(parents=True)
    (settings_dir / "settings.json").write_text(
        json.dumps(
            {
                "env": {
                    "CLAUDE_CODE_USE_VERTEX": "1",
                    "ANTHROPIC_VERTEX_PROJECT_ID": "settings-project",
                    "CLOUD_ML_REGION": "global",
                    "ANTHROPIC_DEFAULT_OPUS_MODEL": "claude-opus-5[1m]",
                }
            }
        ),
        encoding="utf-8",
    )
    (settings_dir / "settings.json").chmod(0o600)
    environment = _mock_environment(
        runner,
        fake_bin,
        mode="trusted",
        extra_environment={
            "HOME": str(fake_home),
            "RETHLAS_RUN_MODE": "core",
            "RETHLAS_MAIN_AGENT": "opus",
            "RETHLAS_CLAUDE_ROOT_PRINT_CMD": "1",
            "CLAUDE_CODE_USE_VERTEX": "1",
            "ANTHROPIC_VERTEX_PROJECT_ID": "inherited-project",
        },
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )

    assert completed.returncode == 1
    assert "Inherited Vertex selector conflicts" in completed.stderr
    assert "Claude root session:" not in completed.stdout


def test_claude_root_injects_only_host_controlled_vertex_thinking_display(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    fake_curl = fake_bin / "curl"
    fake_curl.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_curl.chmod(0o755)
    environment_file = tmp_path / "claude-environment.jsonl"
    environment = _mock_environment(
        runner,
        fake_bin,
        mode="trusted",
        extra_environment={
            "RETHLAS_RUN_MODE": "core",
            "RETHLAS_MAIN_AGENT": "opus",
            "CLAUDE_CODE_USE_VERTEX": "1",
            "ANTHROPIC_VERTEX_PROJECT_ID": "private-project-marker",
            "CLOUD_ML_REGION": "global",
            "ANTHROPIC_DEFAULT_OPUS_MODEL": "claude-opus-5[1m]",
            "MOCK_CLAUDE_ENV_FILE": str(environment_file),
        },
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    projected = [
        json.loads(line) for line in environment_file.read_text().splitlines()
    ]
    assert projected == [
        {
            "CLAUDE_CODE_EXTRA_BODY": (
                '{"thinking":{"type":"adaptive","display":"summarized"}}'
            ),
            "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "48000",
        }
    ]
    assert "Claude thinking display projection: vertex-summarized" in completed.stdout


def test_claude_root_rejects_unbound_vertex_extra_body_before_paid_turn(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    calls_file = tmp_path / "claude-calls.jsonl"
    environment = _mock_environment(
        runner,
        fake_bin,
        mode="trusted",
        extra_environment={
            "RETHLAS_RUN_MODE": "core",
            "RETHLAS_MAIN_AGENT": "opus",
            "RETHLAS_CLAUDE_ROOT_PRINT_CMD": "1",
            "CLAUDE_CODE_USE_VERTEX": "1",
            "ANTHROPIC_VERTEX_PROJECT_ID": "private-project-marker",
            "CLOUD_ML_REGION": "global",
            "ANTHROPIC_DEFAULT_OPUS_MODEL": "claude-opus-5[1m]",
            "CLAUDE_CODE_EXTRA_BODY": '{"temperature":1}',
            "MOCK_CLAUDE_CALLS_FILE": str(calls_file),
        },
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )

    assert completed.returncode == 1
    assert "host-controlled summarized-thinking request body" in completed.stderr
    assert "Claude root session:" not in completed.stdout
    assert not calls_file.exists()
    assert not (runner.parents[1].parent / ".claude_core").exists()


def test_claude_root_rejects_incomplete_vertex_user_settings(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    fake_home = tmp_path / "home"
    settings_dir = fake_home / ".claude"
    settings_dir.mkdir(parents=True)
    settings = settings_dir / "settings.json"
    settings.write_text(
        json.dumps(
            {
                "env": {
                    "CLAUDE_CODE_USE_VERTEX": "1",
                    "ANTHROPIC_VERTEX_PROJECT_ID": "project-id",
                    "CLOUD_ML_REGION": "us-east5",
                }
            }
        ),
        encoding="utf-8",
    )
    settings.chmod(0o600)
    environment = _mock_environment(
        runner,
        fake_bin,
        mode="trusted",
        extra_environment={
            "HOME": str(fake_home),
            "RETHLAS_RUN_MODE": "core",
            "RETHLAS_MAIN_AGENT": "opus",
            "RETHLAS_CLAUDE_ROOT_PRINT_CMD": "1",
        },
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )

    assert completed.returncode == 1
    assert "Could not project the allowlisted Claude Vertex settings" in completed.stderr
    assert "Claude root session:" not in completed.stdout


def test_claude_root_rejects_bytecode_cache_before_paid_root(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    generation_root = runner.parents[1]
    cache_dir = generation_root / "mcp" / "__pycache__"
    cache_dir.mkdir()
    (cache_dir / "server.cpython-test.pyc").write_bytes(b"not executable")
    environment = _mock_environment(
        runner,
        fake_bin,
        mode="trusted",
        extra_environment={
            "RETHLAS_RUN_MODE": "core",
            "RETHLAS_MAIN_AGENT": "opus",
        },
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )

    assert completed.returncode == 1
    assert "rejected Python bytecode before any paid root" in completed.stderr
    assert str(cache_dir) in completed.stderr
    assert "Claude root session:" not in completed.stdout
    assert not (runner.parents[1].parent / ".claude_core").exists()


def test_claude_root_canary_prompt_binds_exact_artifact_paths(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    statement_sha256 = hashlib.sha256(
        (runner.parents[1] / "data" / "example.md").read_bytes()
    ).hexdigest()
    environment = _mock_environment(
        runner,
        fake_bin,
        mode="trusted",
        extra_environment={
            "RETHLAS_RUN_MODE": "core",
            "RETHLAS_MAIN_AGENT": "opus",
            "RETHLAS_CLAUDE_ROOT_PRINT_CMD": "1",
            "RETHLAS_CLAUDE_ROOT_CANARY": "1",
        },
    )
    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert r"results/example/blueprint.md" in completed.stdout
    assert r"sha256=none" in completed.stdout
    assert r"data/example.refs\ \(absent\)" in completed.stdout
    assert r"durable_memory_state=absent" in completed.stdout
    assert statement_sha256 in completed.stdout
    assert r"Never\ use\ Read\ on\ memory/" in completed.stdout
    assert r"only\ memory_search\ in\ this\ logical\ root\ turn" in completed.stdout
    assert r"exact\ completion_handoff" in completed.stdout
    assert r"do\ not\ issue\ a\ second\ relevance\ search" in completed.stdout
    assert r"owner-authorized\ transport\ canary" in completed.stdout
    assert r"Do\ not\ inspect\ CLAUDE.md" in completed.stdout
    assert r"Use\ edit_blueprint\ with\ the\ latest\ receipt\ SHA" in completed.stdout


def test_claude_root_prompt_binds_existing_draft_sha_for_cas_edits(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    draft = runner.parents[1] / "results" / "example" / "blueprint.md"
    draft.parent.mkdir(parents=True)
    draft.write_text("Existing draft.\n", encoding="utf-8")
    draft_sha256 = hashlib.sha256(draft.read_bytes()).hexdigest()
    environment = _mock_environment(
        runner,
        fake_bin,
        mode="trusted",
        extra_environment={
            "RETHLAS_RUN_MODE": "core",
            "RETHLAS_MAIN_AGENT": "fable",
            "RETHLAS_CLAUDE_ROOT_PRINT_CMD": "1",
        },
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert draft_sha256 in completed.stdout
    assert "mcp__rethlas-root__edit_blueprint" in completed.stdout
    assert "--tools Read" in completed.stdout
    assert "--tools Read,Write" not in completed.stdout


def test_claude_root_refuses_before_paid_turn_when_codex_is_logged_out(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    environment = _mock_environment(
        runner,
        fake_bin,
        mode="trusted",
        extra_environment={
            "RETHLAS_RUN_MODE": "core",
            "RETHLAS_MAIN_AGENT": "fable",
            "MOCK_CODEX_LOGGED_IN": "0",
        },
    )
    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )
    assert completed.returncode == 1
    assert "Codex CLI is not logged in" in completed.stderr
    assert "before any paid turn" in completed.stderr
    assert not (runner.parents[1].parent / ".claude_core").exists()


def test_published_claude_root_restart_is_zero_model_noop(
    tmp_path: Path,
) -> None:
    from agents.generation.mcp.proof_context import (
        aggregate_adaptive_context_digest,
        aggregate_context_digest,
        build_item_context,
        parse_blueprint,
    )

    runner, fake_bin = _make_runner_tree(tmp_path)
    generation_root = runner.parents[1]
    statement_path = generation_root / "data" / "example.md"
    statement_sha256 = hashlib.sha256(statement_path.read_bytes()).hexdigest()
    result_dir = generation_root / "results" / "example"
    result_dir.mkdir(parents=True)
    proof = (
        "# theorem thm:main\n\n"
        "<!-- rethlas-depends-on: -->\n"
        "## statement\nS\n\n"
        "## proof\nComplete proof.\n"
    )
    draft_path = result_dir / "blueprint.md"
    verified_path = result_dir / "blueprint_verified.md"
    draft_path.write_text(proof, encoding="utf-8")
    verified_path.write_text(proof, encoding="utf-8")
    manifest = parse_blueprint(proof, target_statement="S")
    item_id = manifest.item_ids[0]
    context = build_item_context(manifest, item_id, max_chars=200_000)
    attestation = {
        "item_id": item_id,
        "verdict": "correct",
        "disposition": "verified",
        "expanded_proof_ids": [],
        "final_round": 0,
        "context_digest": context["digest"],
        "max_chars": 200_000,
    }
    receipt = {
        "schema_version": "rethlas-publication-v2",
        "problem_id": "example",
        "statement_digest": statement_sha256,
        "proof_digest": hashlib.sha256(proof.encode()).hexdigest(),
        "context_digest": aggregate_context_digest(manifest),
        "adaptive_context_digest": aggregate_adaptive_context_digest(
            manifest, [attestation]
        ),
        "item_context_attestations": [attestation],
        "checked_item_ids": [item_id],
        "verified_path": str(verified_path),
        "published_bytes": len(proof.encode()),
    }
    receipt_root = generation_root.parent / ".verification_receipts"
    receipt_root.mkdir()
    (receipt_root / "example.json").write_text(
        json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    calls_file = tmp_path / "terminal-codex-calls.jsonl"
    environment = _mock_environment(
        runner,
        fake_bin,
        mode="trusted",
        extra_environment={
            "RETHLAS_RUN_MODE": "core",
            "RETHLAS_MAIN_AGENT": "fable",
            "MOCK_CODEX_LOGGED_IN": "0",
            "MOCK_CODEX_CALLS_FILE": str(calls_file),
        },
    )
    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "starting zero Claude or Codex turns" in completed.stdout
    assert '"status":"published"' in completed.stdout
    assert not calls_file.exists()


def test_claude_root_refuses_auth_mismatch_before_root_manifest(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    environment = _mock_environment(
        runner,
        fake_bin,
        mode="trusted",
        extra_environment={
            "HOME": str(fake_home),
            "RETHLAS_RUN_MODE": "core",
            "RETHLAS_MAIN_AGENT": "opus",
            "MOCK_CLAUDE_LOGGED_IN": "0",
        },
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )

    assert completed.returncode == 1
    assert "auth/model provider is unavailable" in completed.stderr
    assert "Claude root manifest:" not in completed.stdout
    assert not (runner.parents[1].parent / ".claude_core").exists()


def test_claude_root_refuses_logged_in_but_different_provider(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    environment = _mock_environment(
        runner,
        fake_bin,
        mode="trusted",
        extra_environment={
            "RETHLAS_RUN_MODE": "core",
            "RETHLAS_MAIN_AGENT": "fable",
            "MOCK_CLAUDE_API_PROVIDER": "vertex",
        },
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )

    assert completed.returncode == 1
    assert "auth status does not match the bound provider" in completed.stderr
    assert "Claude root manifest:" not in completed.stdout
    assert not (runner.parents[1].parent / ".claude_core").exists()


def test_max_diversity_service_profile_mismatch_starts_zero_paid_root(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    fake_curl = fake_bin / "curl"
    fake_curl.write_text(
        """#!/bin/sh
case "$*" in
  *profile*)
    printf '%s' '{"schema_version":"rethlas_verifier_profile_v1","service_version":"0.4.0","profile":"compatible","passes":[],"automatic_tiebreaker":false,"fallback_policy":"forbid"}'
    ;;
  *) printf '%s' '{"status":"ok"}' ;;
esac
""",
        encoding="utf-8",
    )
    fake_curl.chmod(0o755)
    claude_calls = tmp_path / "claude-model-calls.jsonl"
    environment = _mock_environment(
        runner,
        fake_bin,
        mode="trusted",
        extra_environment={
            "RETHLAS_RUN_MODE": "core",
            "RETHLAS_MAIN_AGENT": "opus",
            "RETHLAS_MODEL_POLICY_PROFILE": "max_diversity",
            "MOCK_CLAUDE_CALLS_FILE": str(claude_calls),
        },
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 1
    assert "does not match model-policy profile=max_diversity" in completed.stderr
    assert not claude_calls.exists()
    assert not (runner.parents[1].parent / ".claude_core").exists()


def test_claude_core_executes_one_noninteractive_persistent_turn(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    generation_root = runner.parents[1]
    statement_sha256 = hashlib.sha256(
        (generation_root / "data" / "example.md").read_bytes()
    ).hexdigest()
    candidate_id = "manual_pro_candidate"
    candidate_content = "Current statement candidate sentinel.\n"
    candidate_sha256 = hashlib.sha256(candidate_content.encode()).hexdigest()
    ingest = subprocess.run(
        [
            str(fake_bin / "python"),
            "-I",
            "-B",
            str(generation_root.parent / "claude_core.py"),
            "--ingest-reference-candidate",
            "example",
            statement_sha256,
            candidate_id,
        ],
        cwd=generation_root,
        input=candidate_content,
        text=True,
        capture_output=True,
        check=False,
    )
    assert ingest.returncode == 0, ingest.stdout + ingest.stderr
    candidate_relative = Path(
        ".claude_core_inputs",
        "reference_candidates",
        "example",
        statement_sha256,
        candidate_id,
        f"{candidate_sha256}.md",
    )
    candidate_projection = generation_root / candidate_relative
    candidate_projection_root = candidate_projection.parents[1]
    assert candidate_projection.read_text(encoding="utf-8") == candidate_content
    fake_curl = fake_bin / "curl"
    fake_curl.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_curl.chmod(0o755)
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    claude_calls = tmp_path / "claude-calls.jsonl"
    environment = _mock_environment(
        runner,
        fake_bin,
        mode="trusted",
        extra_environment={
            "HOME": str(fake_home),
            "RETHLAS_RUN_MODE": "core",
            "RETHLAS_MAIN_AGENT": "opus",
            "RETHLAS_CLAUDE_ROOT_CANARY": "1",
            "MOCK_CLAUDE_CALLS_FILE": str(claude_calls),
        },
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "mock Claude turn complete" in completed.stdout
    calls = [json.loads(line) for line in claude_calls.read_text().splitlines()]
    assert len(calls) == 1
    arguments = calls[0]
    assert "--print" in arguments
    assert arguments[arguments.index("--output-format") + 1] == "stream-json"
    assert "--session-id" in arguments
    assert "--resume" not in arguments
    assert "reference_candidate_inventory=" in arguments[-1]
    assert "may not be silently dropped" in arguments[-1]
    assert "required_marker plus that exact path" in arguments[-1]
    assert candidate_relative.as_posix() in arguments[-1]
    assert f"[reference_candidate:{candidate_id}]" in arguments[-1]
    add_dirs = [
        Path(arguments[index + 1])
        for index, argument in enumerate(arguments[:-1])
        if argument == "--add-dir"
    ]
    assert add_dirs == [
        generation_root / "data",
        generation_root / "results",
        candidate_projection_root,
    ]
    assert generation_root / ".claude_core_inputs" not in add_dirs
    manifests = list(
        (runner.parents[1].parent / ".claude_core" / "example" / "roots").glob(
            "*/manifest.json"
        )
    )
    assert len(manifests) == 1


def test_claude_root_stream_projection_filters_thinking_flood(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    fake_curl = fake_bin / "curl"
    fake_curl.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_curl.chmod(0o755)
    environment = _mock_environment(
        runner,
        fake_bin,
        mode="trusted",
        extra_environment={
            "RETHLAS_RUN_MODE": "core",
            "RETHLAS_MAIN_AGENT": "opus",
            "RETHLAS_CLAUDE_ROOT_CANARY": "1",
            "MOCK_CLAUDE_THINKING_EVENTS": "500",
            "MOCK_CLAUDE_THINKING_MARKER": "private-thinking-must-not-project",
        },
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "mock Claude turn complete" in completed.stdout
    assert "thinking_tokens" not in completed.stdout
    assert "private-thinking-must-not-project" not in completed.stdout
    assert len(completed.stdout.encode("utf-8")) < 50_000


@pytest.mark.parametrize("markerless", [False, True])
def test_claude_root_auto_continues_exact_max_output_error_in_same_session(
    tmp_path: Path, markerless: bool,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    fake_curl = fake_bin / "curl"
    fake_curl.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_curl.chmod(0o755)
    calls_file = tmp_path / "claude-calls.jsonl"
    state_file = tmp_path / "claude-max-output-state"
    extra_environment = {
        "RETHLAS_RUN_MODE": "core",
        "RETHLAS_MAIN_AGENT": "opus",
        "RETHLAS_CLAUDE_ROOT_CANARY": "1",
        "MOCK_CLAUDE_CALLS_FILE": str(calls_file),
        "MOCK_CLAUDE_MAX_OUTPUT_FAILURES": "2",
        "MOCK_CLAUDE_MAX_OUTPUT_STATE": str(state_file),
    }
    if markerless:
        extra_environment["MOCK_CLAUDE_MAX_OUTPUT_MARKERLESS"] = "1"
    environment = _mock_environment(
        runner,
        fake_bin,
        mode="trusted",
        extra_environment=extra_environment,
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    calls = [json.loads(line) for line in calls_file.read_text().splitlines()]
    assert len(calls) == 3
    first_session = calls[0][calls[0].index("--session-id") + 1]
    assert "--resume" not in calls[0]
    for continuation in calls[1:]:
        assert "--session-id" not in continuation
        assert continuation[continuation.index("--resume") + 1] == first_session
        assert "48000-token response segment" in continuation[-1]
        assert "not a cumulative token budget" in continuation[-1]
        assert "at max effort" in continuation[-1]
        assert "duplicate a tool side effect" in continuation[-1]
    assert completed.stderr.count("resuming the same session") == 2
    assert completed.stderr.count("cumulative output unbounded") == 2
    assert "mock Claude turn complete" in completed.stdout
    assert not list((runner.parent.parent / ".runner-tmp").glob("rethlas-claude-root.*"))


def test_claude_root_continuation_executes_the_same_cli_snapshot_after_upgrade(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    fake_curl = fake_bin / "curl"
    fake_curl.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_curl.chmod(0o755)
    claude_origin = fake_bin / "claude"
    original = claude_origin.read_text(encoding="utf-8")
    print_branch = 'if "--print" in sys.argv:\n'
    entered = tmp_path / "claude-a-entered.marker"
    release = tmp_path / "claude-a-release.marker"
    replacement_executed = tmp_path / "claude-b-executed.marker"
    barrier = (
        "    entered = os.environ.get('MOCK_CLAUDE_A_ENTERED')\n"
        "    release = os.environ.get('MOCK_CLAUDE_A_RELEASE')\n"
        "    if entered and release:\n"
        "        import pathlib, time\n"
        "        pathlib.Path(entered).write_text('entered', encoding='utf-8')\n"
        "        while not pathlib.Path(release).exists():\n"
        "            time.sleep(0.01)\n"
    )
    source_a = original.replace(print_branch, print_branch + barrier, 1)
    replacement_probe = (
        "    replacement_marker = os.environ.get('MOCK_CLAUDE_B_EXECUTED')\n"
        "    if replacement_marker:\n"
        "        import pathlib\n"
        "        pathlib.Path(replacement_marker).write_text("
        "'executed', encoding='utf-8')\n"
    )
    source_b = original.replace(print_branch, print_branch + replacement_probe, 1)
    claude_origin.write_text(source_a, encoding="utf-8")
    claude_origin.chmod(0o755)
    replacement = tmp_path / "claude-b"
    replacement.write_text(source_b, encoding="utf-8")
    replacement.chmod(0o755)
    calls_file = tmp_path / "claude-snapshot-calls.jsonl"
    state_file = tmp_path / "claude-snapshot-state"
    environment = _mock_environment(
        runner,
        fake_bin,
        mode="trusted",
        extra_environment={
            "RETHLAS_RUN_MODE": "core",
            "RETHLAS_MAIN_AGENT": "opus",
            "MOCK_CLAUDE_CALLS_FILE": str(calls_file),
            "MOCK_CLAUDE_MAX_OUTPUT_FAILURES": "1",
            "MOCK_CLAUDE_MAX_OUTPUT_STATE": str(state_file),
            "MOCK_CLAUDE_A_ENTERED": str(entered),
            "MOCK_CLAUDE_A_RELEASE": str(release),
            "MOCK_CLAUDE_B_EXECUTED": str(replacement_executed),
        },
    )
    process = subprocess.Popen(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 15
        while not entered.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert entered.exists(), "first frozen Claude CLI turn never reached barrier"
        os.replace(replacement, claude_origin)
        release.write_text("continue", encoding="utf-8")
        stdout, stderr = process.communicate(timeout=30)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)

    assert process.returncode == 0, stdout + stderr
    assert not replacement_executed.exists()
    assert len(calls_file.read_text(encoding="utf-8").splitlines()) == 2
    manifests = list(
        (runner.parents[1].parent / ".claude_core" / "example" / "roots").glob(
            "*/manifest.json"
        )
    )
    assert len(manifests) == 1
    manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
    assert manifest["claude_cli_sha256"] == hashlib.sha256(
        source_a.encode("utf-8")
    ).hexdigest()


@pytest.mark.parametrize("preceded_by_max_output_event", [False, True])
def test_claude_root_does_not_auto_continue_generic_provider_error(
    tmp_path: Path, preceded_by_max_output_event: bool
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    fake_curl = fake_bin / "curl"
    fake_curl.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_curl.chmod(0o755)
    calls_file = tmp_path / "claude-calls.jsonl"
    extra_environment = {
        "RETHLAS_RUN_MODE": "core",
        "RETHLAS_MAIN_AGENT": "opus",
        "MOCK_CLAUDE_CALLS_FILE": str(calls_file),
        "MOCK_CLAUDE_GENERIC_ERROR": "1",
    }
    if preceded_by_max_output_event:
        extra_environment["MOCK_CLAUDE_RECOVERED_MAX_OUTPUT"] = "1"
    environment = _mock_environment(
        runner,
        fake_bin,
        mode="trusted",
        extra_environment=extra_environment,
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 1
    calls = [json.loads(line) for line in calls_file.read_text().splitlines()]
    assert len(calls) == 1
    assert "resuming the same session" not in completed.stderr


def test_claude_root_does_not_continue_recovered_max_output_event(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    fake_curl = fake_bin / "curl"
    fake_curl.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_curl.chmod(0o755)
    calls_file = tmp_path / "claude-calls.jsonl"
    environment = _mock_environment(
        runner,
        fake_bin,
        mode="trusted",
        extra_environment={
            "RETHLAS_RUN_MODE": "core",
            "RETHLAS_MAIN_AGENT": "opus",
            "MOCK_CLAUDE_CALLS_FILE": str(calls_file),
            "MOCK_CLAUDE_RECOVERED_MAX_OUTPUT": "1",
        },
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    calls = [json.loads(line) for line in calls_file.read_text().splitlines()]
    assert len(calls) == 1
    assert "resuming the same session" not in completed.stderr


def test_reviewed_mode_rejects_claude_root_before_run_id_or_models(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    calls_file = tmp_path / "reviewed-scout-calls.jsonl"
    environment = _mock_environment(
        runner,
        fake_bin,
        mode="forged",
        extra_environment={
            "RETHLAS_RUN_MODE": "prompt",
            "MOCK_CODEX_CALLS_FILE": str(calls_file),
        },
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        input="2\n2\n",
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )

    assert completed.returncode == 1
    assert "not yet admitted to reviewed mode" in completed.stderr
    assert "Enter a hot-join run id" not in completed.stderr
    assert not calls_file.exists()


def _prepare_mock_claude_root(
    runner: Path,
    *,
    session_id: str,
    model: str = "claude-opus-5",
    orchestration_mode: str = "single_root",
    takeover_from: str | None = None,
) -> str:
    generation_root = runner.parents[1]
    problem = generation_root / "data" / "example.md"
    statement_sha256 = hashlib.sha256(problem.read_bytes()).hexdigest()
    python_runtime_sha256 = hashlib.sha256(
        (runner.parents[2] / ".generation-venv" / "bin" / "python").read_bytes()
    ).hexdigest()
    root_launcher_sha256 = hashlib.sha256(
        runner.with_name("run_claude_core.sh").read_bytes()
    ).hexdigest()
    preparation_environment = dict(os.environ)
    preparation_environment.update(
        {
            "RETHLAS_CLAUDE_PINNED_PYTHON_SHA256": python_runtime_sha256,
            "RETHLAS_CLAUDE_ROOT_LAUNCHER_SHA256": root_launcher_sha256,
        }
    )
    command = [
            sys.executable,
            "-I",
            "-B",
            str(generation_root.parent / "claude_core.py"),
            "--prepare-root",
            "example",
            statement_sha256,
            session_id,
            model,
            ("claude-opus-5[1m]" if model == "claude-opus-5" else model),
            "vertex",
            "2" * 64,
            "1" * 64,
            "test-claude-2.1.246",
            ("1000000" if model == "claude-opus-5" else "200000"),
            python_runtime_sha256,
            root_launcher_sha256,
            orchestration_mode,
        ]
    if takeover_from is not None:
        command.append(takeover_from)
    completed = subprocess.run(
        command,
        cwd=generation_root,
        env=preparation_environment,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    return statement_sha256


def test_claude_root_resume_reuses_exact_session_id(tmp_path: Path) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    session_id = "12345678-1234-4123-8123-123456789abc"
    statement_sha256 = _prepare_mock_claude_root(
        runner, session_id=session_id
    )
    environment = _mock_environment(
        runner,
        fake_bin,
        mode="trusted",
        extra_environment={
            "RETHLAS_RUN_MODE": "core",
            "RETHLAS_MAIN_AGENT": "opus",
            "RETHLAS_CLAUDE_ROOT_PRINT_CMD": "1",
            "RETHLAS_CLAUDE_ROOT_SESSION_ID": session_id,
        },
    )
    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert f"--resume {session_id}" in completed.stdout
    assert "Claude root CLI model: claude-opus-5[1m]" in completed.stdout
    assert "--session-id" not in completed.stdout
    assert completed.stdout.index("--mcp-config") < completed.stdout.index(
        "--resume"
    )
    assert "do\\ not\\ restart\\ route\\ discovery" in completed.stdout
    assert statement_sha256 in completed.stdout


def test_claude_root_resume_rejects_a_replaced_launcher_epoch(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    session_id = "12345678-1234-4123-8123-123456789abc"
    _prepare_mock_claude_root(runner, session_id=session_id)
    launcher = runner.with_name("run_claude_core.sh")
    launcher.write_bytes(launcher.read_bytes() + b"\n# deployment-b\n")
    launcher.chmod(0o755)
    environment = _mock_environment(
        runner,
        fake_bin,
        mode="trusted",
        extra_environment={
            "RETHLAS_RUN_MODE": "core",
            "RETHLAS_MAIN_AGENT": "opus",
            "RETHLAS_CLAUDE_ROOT_PRINT_CMD": "1",
            "RETHLAS_CLAUDE_ROOT_SESSION_ID": session_id,
        },
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 1
    assert "launcher execution epoch differs" in completed.stderr
    assert "--resume" not in completed.stdout


def test_claude_root_resume_reports_host_source_drift_precisely(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    session_id = "12345678-1234-4123-8123-123456789abc"
    _prepare_mock_claude_root(runner, session_id=session_id)
    core_source = runner.parents[1].parent / "claude_core.py"
    core_source.write_bytes(core_source.read_bytes() + b"\n# deployment-b\n")
    environment = _mock_environment(
        runner,
        fake_bin,
        mode="trusted",
        extra_environment={
            "RETHLAS_RUN_MODE": "core",
            "RETHLAS_MAIN_AGENT": "opus",
            "RETHLAS_CLAUDE_ROOT_PRINT_CMD": "1",
            "RETHLAS_CLAUDE_ROOT_SESSION_ID": session_id,
        },
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 1
    assert "active root host source differs" in completed.stderr
    assert "provider binding" not in completed.stderr
    assert "--resume" not in completed.stdout


def test_claude_owner_migration_launcher_supplies_the_replacement_epoch(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    launcher = runner.with_name("run_claude_core.sh")
    core_source = runner.parents[1].parent / "claude_core.py"
    old_root = "12345678-1234-4123-8123-123456789abc"
    new_root = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    statement_sha256 = _prepare_mock_claude_root(
        runner,
        session_id=old_root,
        orchestration_mode="opus_sol_council_v2",
    )
    source_a_sha256 = hashlib.sha256(core_source.read_bytes()).hexdigest()
    state_root = runner.parents[1].parent / ".claude_core"
    council_id = "council_" + "d" * 32
    council_dir = state_root / "example" / "councils" / council_id
    council_dir.parent.mkdir(mode=0o700)
    council_dir.mkdir(mode=0o700)
    pointer_path = (
        state_root / "example" / "roots" / old_root / "route_council.json"
    )
    pointer = {
        "schema_version": "rethlas_route_council_pointer_v2",
        "pointer_version": 1,
        "problem_id": "example",
        "statement_sha256": statement_sha256,
        "root_session_id": old_root,
        "council_round": 1,
        "council_id": council_id,
        "base_frontier_sha256": "1" * 64,
        "opus_plan_sha256": "2" * 64,
        "prior_context_sha256": "3" * 64,
        "prior_failure_receipt_sha256": None,
        "host_source_sha256": source_a_sha256,
        "predecessor_root_session_id": None,
        "predecessor_council_id": None,
        "predecessor_pointer_sha256": None,
        "state": "active",
        "final_plan_sha256": None,
        "acceptance_sha256": None,
        "checkpoint_sha256": None,
        "cohort_id": None,
        "updated_at_unix": time.time(),
    }
    pointer_path.write_text(
        json.dumps(pointer, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    pointer_path.chmod(0o400)

    source_a = core_source.read_text(encoding="utf-8")
    main_needle = '\nif __name__ == "__main__":\n    main()\n'
    owner_probe = '''
if __name__ == "__main__" and sys.argv[1:2] == ["--migrate-stale-route-council"]:
    _owner_turn_only_environment = (
        "RETHLAS_MODEL_POLICY_PROFILE",
        "CLAUDE_CODE_MAX_OUTPUT_TOKENS",
    )
    if any(
        os.environ.get(_owner_key) is not None
        for _owner_key in _owner_turn_only_environment
    ):
        raise SystemExit("owner migration inherited model-turn configuration")
    _owner_legacy_module = _legacy()
    if (
        _RUNTIME_BUNDLE_DIR is None
        or Path(_owner_legacy_module.__file__).resolve().parent
        != _RUNTIME_MCP_ROOT.resolve()
    ):
        raise SystemExit("owner migration did not load its frozen dependency bundle")
    _owner_entered = os.environ.get("OWNER_MIGRATION_ENTERED")
    _owner_release = os.environ.get("OWNER_MIGRATION_RELEASE")
    if _owner_entered and _owner_release:
        Path(_owner_entered).write_text("entered", encoding="utf-8")
        while not Path(_owner_release).exists():
            time.sleep(0.01)
'''
    assert main_needle in source_a
    source_b = source_a.replace(main_needle, owner_probe + main_needle, 1)
    source_b += "\n# deployment-b\n"
    core_source.write_text(source_b, encoding="utf-8")
    source_b_sha256 = hashlib.sha256(core_source.read_bytes()).hexdigest()
    assert source_b_sha256 != source_a_sha256
    entered = tmp_path / "owner-migration-entered.marker"
    release = tmp_path / "owner-migration-release.marker"
    environment = _mock_environment(
        runner,
        fake_bin,
        mode="trusted",
        extra_environment={
            "RETHLAS_RUN_MODE": "core",
            "RETHLAS_MAIN_AGENT": "gpt-sol",
            "RETHLAS_MODEL_POLICY_PROFILE": "invalid-admin-leftover",
            "RETHLAS_CLAUDE_CONTEXT_WINDOW": "invalid",
            "RETHLAS_CLAUDE_ROOT_PRINT_CMD": "invalid",
            "RETHLAS_CLAUDE_ROOT_CANARY": "invalid",
            "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "1",
            "OWNER_MIGRATION_ENTERED": str(entered),
            "OWNER_MIGRATION_RELEASE": str(release),
        },
    )
    for key in (
        "RETHLAS_CLAUDE_PINNED_PYTHON_BIN",
        "RETHLAS_CLAUDE_PINNED_PYTHON_SHA256",
        "RETHLAS_CLAUDE_ROOT_LAUNCHER_SHA256",
    ):
        environment.pop(key, None)
    reason = "Retire the stale council through the authenticated owner entry."
    migration_command = [
        str(launcher),
        "--migrate-stale-route-council",
        "example",
        statement_sha256,
        old_root,
        reason,
        "--confirm-source-drift",
    ]
    fence_path = (
        state_root
        / "example"
        / "roots"
        / old_root
        / "source_drift_fence.json"
    )

    dependency = core_source.parent / "generation" / "mcp" / "proof_context.py"
    dependency_a = dependency.read_bytes()
    dependency.write_bytes(dependency_a + b"\n# unmatched-deployment\n")
    dependency_mismatch = subprocess.run(
        migration_command,
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert dependency_mismatch.returncode == 70
    assert "runtime dependency differs from Core binding" in (
        dependency_mismatch.stdout + dependency_mismatch.stderr
    )
    assert not fence_path.exists()
    dependency.write_bytes(dependency_a)

    process = subprocess.Popen(
        migration_command,
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 15
        while not entered.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert entered.exists(), "owner migration never loaded its frozen closure"
        source_c = source_b + "\n# deployment-c\n"
        replacement = tmp_path / "claude-core-deployment-c.py"
        replacement.write_text(source_c, encoding="utf-8")
        os.replace(replacement, core_source)
        source_c_sha256 = hashlib.sha256(core_source.read_bytes()).hexdigest()
        release.write_text("continue", encoding="utf-8")
        stale_stdout, stale_stderr = process.communicate(timeout=30)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)
    assert process.returncode == 70, stale_stdout + stale_stderr
    assert "Claude root host source drift" in (stale_stdout + stale_stderr)
    assert not fence_path.exists()
    assert json.loads(pointer_path.read_text(encoding="utf-8"))["state"] == "active"

    completed = subprocess.run(
        migration_command,
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    migration = json.loads(completed.stdout)
    assert migration["status"] == "source_drift_blocked"
    assert migration["old_host_source_sha256"] == source_a_sha256
    assert migration["replacement_host_source_sha256"] == source_c_sha256
    assert migration["schema_version"] == (
        "rethlas_route_council_source_drift_termination_v2"
    )

    successor = _prepare_mock_claude_root(
        runner,
        session_id=new_root,
        orchestration_mode="opus_sol_council_v2",
        takeover_from=old_root,
    )
    assert successor == statement_sha256
    active = json.loads(
        (state_root / "example" / "active_root.json").read_text(
            encoding="utf-8"
        )
    )
    assert active["root_session_id"] == new_root
    assert active["previous_root_session_id"] == old_root


def test_opus_sol_council_resume_requires_and_preserves_council_mode(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    session_id = "12345678-1234-4123-8123-123456789abc"
    _prepare_mock_claude_root(
        runner,
        session_id=session_id,
        orchestration_mode="opus_sol_council_v2",
    )
    environment = _mock_environment(
        runner,
        fake_bin,
        mode="trusted",
        extra_environment={
            "RETHLAS_RUN_MODE": "core",
            "RETHLAS_MAIN_AGENT": "opus-sol-council",
            "RETHLAS_CLAUDE_ROOT_PRINT_CMD": "1",
            "RETHLAS_CLAUDE_ROOT_SESSION_ID": session_id,
        },
    )
    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert f"--resume {session_id}" in completed.stdout
    assert "Claude orchestration mode: opus_sol_council_v2" in completed.stdout
    assert "resume\\ its\\ exact\\ durable\\ phase" in completed.stdout


def test_claude_root_takeover_dry_run_fences_nothing_until_launch(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    old_session = "12345678-1234-4123-8123-123456789abc"
    statement_sha256 = _prepare_mock_claude_root(
        runner, session_id=old_session
    )
    environment = _mock_environment(
        runner,
        fake_bin,
        mode="trusted",
        extra_environment={
            "RETHLAS_RUN_MODE": "core",
            "RETHLAS_MAIN_AGENT": "fable",
            "RETHLAS_CLAUDE_ROOT_PRINT_CMD": "1",
            "RETHLAS_CLAUDE_ROOT_TAKEOVER_FROM": old_session,
            "RETHLAS_CLAUDE_ROOT_OWNER_PROMPT": (
                "Rehydrate the exact named durable dossier."
            ),
        },
    )
    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "--session-id" in completed.stdout
    assert "--resume" not in completed.stdout
    assert f"prior\\ root\\ {old_session}\\ is\\ fenced" in completed.stdout
    assert (
        "Rehydrate\\ the\\ exact\\ named\\ durable\\ dossier"
        in completed.stdout
    )
    assert statement_sha256 in completed.stdout
    core_source = runner.parents[1].parent / "claude_core.py"
    lookup = subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            str(core_source),
            "--get-active-root",
            "example",
            statement_sha256,
        ],
        cwd=runner.parents[1],
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )
    assert lookup.returncode == 0, lookup.stdout + lookup.stderr
    assert json.loads(lookup.stdout)["root_session_id"] == old_session


def test_claude_root_takeover_dry_run_replays_committed_successor(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    old_session = "12345678-1234-4123-8123-123456789abc"
    new_session = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    _prepare_mock_claude_root(runner, session_id=old_session)
    statement_sha256 = _prepare_mock_claude_root(
        runner,
        session_id=new_session,
        model="claude-fable-5",
        takeover_from=old_session,
    )
    environment = _mock_environment(
        runner,
        fake_bin,
        mode="trusted",
        extra_environment={
            "RETHLAS_RUN_MODE": "core",
            "RETHLAS_MAIN_AGENT": "fable",
            "RETHLAS_CLAUDE_ROOT_PRINT_CMD": "1",
            "RETHLAS_CLAUDE_ROOT_SESSION_ID": new_session,
            "RETHLAS_CLAUDE_ROOT_TAKEOVER_FROM": old_session,
        },
    )
    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert f"--session-id {new_session}" in completed.stdout
    assert "--resume" not in completed.stdout
    assert f"prior\\ root\\ {old_session}\\ is\\ fenced" in completed.stdout
    assert statement_sha256 in completed.stdout


@pytest.mark.parametrize(
    ("retrieval_section", "expected_mode", "retrieval_prompt"),
    [
        (
            "",
            "disabled",
            "The SHA-bound problem does not permit external retrieval.",
        ),
        (
            "\n\n## Retrieval restriction\n\n"
            "Matlas and arXiv retrieval are permitted. Use no arXiv source "
            "whose initial submission date is later than 2026-06-26.\n",
            "matlas_arxiv",
            "The SHA-bound problem explicitly permits only the dedicated",
        ),
    ],
)
@pytest.mark.parametrize(
    "descriptor_bound", [False, True], ids=["pathname", "pinned-fds"]
)
@pytest.mark.skipif(
    not _LINUX_COHORT_NAMESPACE_AVAILABLE,
    reason=(
        "host kernel denies the unprivileged mount/PID namespace required "
        "by the production Linux cohort capsule"
    ),
)
def test_host_validated_claude_plan_runs_one_sol_cohort_executor(
    tmp_path: Path,
    retrieval_section: str,
    expected_mode: str,
    retrieval_prompt: str,
    descriptor_bound: bool,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    generation_root = runner.parents[1]
    problem = generation_root / "data" / "example.md"
    problem.write_text("S" + retrieval_section, encoding="utf-8")
    statement_sha256 = hashlib.sha256(problem.read_bytes()).hexdigest()
    root_session_id = "12345678-1234-4123-8123-123456789abc"
    _prepare_mock_claude_root(runner, session_id=root_session_id)
    plan_set = {
        "schema_version": "rethlas_claude_plan_set_v1",
        "problem_id": "example",
        "statement_sha256": statement_sha256,
        "root_session_id": root_session_id,
        "plans": [
            {
                "plan_id": f"route_{index}",
                "mechanism": f"mechanism {index}",
                "scope": f"scope {index}",
                "discriminating_test": f"kill test {index}",
                "plan_summary": f"summary {index}",
                "subgoals": [f"subgoal {index}"],
                "motivation": [f"motivation {index}"],
            }
            for index in range(1, 4)
        ],
    }
    plan_bytes = (
        json.dumps(
            plan_set,
            allow_nan=False,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode()
    plan_sha256 = hashlib.sha256(plan_bytes).hexdigest()
    plan_dir = generation_root / ".claude_core_inputs" / "example" / "test"
    plan_dir.mkdir(parents=True, mode=0o700)
    plan_path = plan_dir / f"plan_{plan_sha256}.json"
    plan_path.write_bytes(plan_bytes)
    plan_path.chmod(0o400)
    candidate_projection_root = (
        generation_root
        / ".claude_core_inputs"
        / "reference_candidates"
    )
    current_candidate = (
        candidate_projection_root
        / "example"
        / statement_sha256
        / "current_candidate"
        / ("1" * 64 + ".md")
    )
    other_statement_candidate = (
        candidate_projection_root
        / "example"
        / ("f" * 64)
        / "other_statement_candidate"
        / ("2" * 64 + ".md")
    )
    other_problem_candidate = (
        candidate_projection_root
        / "other"
        / statement_sha256
        / "other_problem_candidate"
        / ("3" * 64 + ".md")
    )
    for candidate, content in (
        (current_candidate, "current statement candidate sentinel\n"),
        (other_statement_candidate, "other statement candidate sentinel\n"),
        (other_problem_candidate, "other problem candidate sentinel\n"),
    ):
        candidate.parent.mkdir(parents=True, mode=0o700)
        candidate.write_text(content, encoding="utf-8")
        candidate.chmod(0o400)
    (generation_root / "data" / "other.md").write_text(
        "cross-problem sentinel", encoding="utf-8"
    )
    for relative in (
        Path("memory/example/current.txt"),
        Path("results/example/current.txt"),
        Path("memory/other/sentinel.txt"),
        Path("results/other/sentinel.txt"),
        Path("logs/other/sentinel.txt"),
        Path(".claude_core_inputs/other/sentinel.txt"),
    ):
        target = generation_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("sentinel", encoding="utf-8")
    host_claude_state = generation_root.parent / ".claude_core" / "other"
    host_claude_state.mkdir(parents=True)
    (host_claude_state / "sentinel.txt").write_text("sentinel", encoding="utf-8")
    repository_git = generation_root.parents[1] / ".git"
    repository_git.mkdir(exist_ok=True)
    (repository_git / "sentinel").write_text("sentinel", encoding="utf-8")
    calls_file = tmp_path / "claude-cohort-codex-calls.jsonl"
    isolation_probe = tmp_path / "claude-cohort-isolation.json"
    environment = _mock_environment(
        runner,
        fake_bin,
        mode="forged",
        extra_environment={
            "RETHLAS_RUN_MODE": "core",
            "RETHLAS_MAIN_AGENT": "gpt-sol",
            "RETHLAS_EXTERNAL_PLAN_SET": str(plan_path),
                "RETHLAS_EXTERNAL_PLAN_SHA256": plan_sha256,
                "RETHLAS_LEGACY_STOP_AFTER_CURRENT_COHORT": "1",
                # A real cohort inherits these two authenticated execution-
                # epoch bindings from its Claude root launcher.
                "RETHLAS_CLAUDE_PINNED_PYTHON_SHA256": hashlib.sha256(
                    (
                        generation_root.parent
                        / ".generation-venv"
                        / "bin"
                        / "python"
                    ).read_bytes()
                ).hexdigest(),
                "RETHLAS_CLAUDE_ROOT_LAUNCHER_SHA256": hashlib.sha256(
                    runner.with_name("run_claude_core.sh").read_bytes()
                ).hexdigest(),
                "MOCK_CODEX_CALLS_FILE": str(calls_file),
            "MOCK_CODEX_ISOLATION_PROBE_FILE": str(isolation_probe),
            "MOCK_CODEX_EXPECTED_PLAN": str(plan_path.relative_to(generation_root)),
            "MOCK_CODEX_EXPECTED_CANDIDATE": str(
                current_candidate.relative_to(generation_root)
            ),
            "MOCK_CODEX_OTHER_STATEMENT_CANDIDATE": str(
                other_statement_candidate.relative_to(generation_root)
            ),
            "MOCK_CODEX_OTHER_PROBLEM_CANDIDATE": str(
                other_problem_candidate.relative_to(generation_root)
            ),
        },
    )
    isolated_home = tmp_path / "external-home"
    environment["HOME"] = str(isolated_home)
    (isolated_home / ".codex").mkdir(parents=True, exist_ok=True)
    (isolated_home / ".codex" / "history.jsonl").write_text(
        "old codex context", encoding="utf-8"
    )
    host_auth = isolated_home / ".codex" / "auth.json"
    host_auth.write_text("host auth sentinel", encoding="utf-8")
    (isolated_home / ".codex" / "models_cache.json").write_text(
        "host model-cache sentinel", encoding="utf-8"
    )
    (isolated_home / ".claude" / "projects").mkdir(parents=True)
    (isolated_home / ".claude" / "projects" / "old.jsonl").write_text(
        "old claude context", encoding="utf-8"
    )
    output_directory = generation_root / "results" / "example"
    if output_directory.exists():
        shutil.rmtree(output_directory)

    inherited_descriptors: list[int] = []
    if descriptor_bound:
        codex_path = fake_bin / "codex"
        host_source_origin = generation_root.parent / "claude_core.py"
        host_source_snapshot = tmp_path / "cohort-host-source.py"
        host_source_snapshot.write_bytes(host_source_origin.read_bytes())
        host_source_snapshot.chmod(0o400)
        codex_descriptor = os.open(codex_path, os.O_RDONLY)
        source_descriptor = os.open(host_source_snapshot, os.O_RDONLY)
        # Darwin's /dev/fd path duplicates the shared file description. The
        # production readers must use positional reads and ignore this offset.
        os.lseek(codex_descriptor, 0, os.SEEK_END)
        os.lseek(source_descriptor, 0, os.SEEK_END)
        inherited_descriptors.extend((codex_descriptor, source_descriptor))
        environment.update(
            {
                "RETHLAS_COHORT_CODEX_BIN": str(codex_path.resolve()),
                "RETHLAS_COHORT_CODEX_FD": str(codex_descriptor),
                "RETHLAS_COHORT_CODEX_SHA256": hashlib.sha256(
                    codex_path.read_bytes()
                ).hexdigest(),
                "RETHLAS_COHORT_HOST_SOURCE_FD": str(source_descriptor),
                "RETHLAS_COHORT_HOST_SOURCE_ORIGIN": str(
                    host_source_origin.resolve()
                ),
                "RETHLAS_COHORT_HOST_SOURCE_SHA256": hashlib.sha256(
                    host_source_snapshot.read_bytes()
                ).hexdigest(),
                "RETHLAS_COHORT_HOST_SOURCE_SNAPSHOT": str(
                    host_source_snapshot.resolve()
                ),
            }
        )
    try:
        completed = subprocess.run(
            [str(runner)],
            cwd=runner.parent.parent,
            env=environment,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
            pass_fds=tuple(inherited_descriptors),
        )
    finally:
        for descriptor in inherited_descriptors:
            os.close(descriptor)

    assert completed.returncode == 1, completed.stdout + completed.stderr
    assert "no trusted frontier progress" in completed.stderr
    assert output_directory.is_dir() and not output_directory.is_symlink()
    assert f"Accepted Claude root plan set: {plan_sha256}" in completed.stdout
    assert f"Claude cohort retrieval mode: {expected_mode}" in completed.stdout
    calls = [json.loads(line) for line in calls_file.read_text().splitlines()]
    root_call = next(call for call in calls if "exec" in call)
    assert root_call[root_call.index("-m") + 1] == "gpt-5.6-sol"
    prompt = root_call[-1]
    assert "bounded cohort executor for a persistent Claude canonical root" in prompt
    assert plan_sha256 in prompt
    assert "Skip fresh route generation" in prompt
    assert "items=[]" in prompt
    assert "Do not manually reproduce their JSON" in prompt
    assert "per-problem filesystem capsule" in prompt
    assert "declared SHA-bound reference-candidate projection" in prompt
    assert "Never inspect a parent directory" in prompt
    assert f"statement_bound_retrieval_mode={expected_mode}" in prompt
    assert retrieval_prompt in prompt
    web_configs = [
        value
        for index, value in enumerate(root_call)
        if index > 0
        and root_call[index - 1] == "--config"
        and value.startswith("web_search=")
    ]
    assert web_configs == ['web_search="disabled"']
    login_shell_configs = [
        value
        for index, value in enumerate(root_call)
        if index > 0
        and root_call[index - 1] == "--config"
        and value.startswith("allow_login_shell=")
    ]
    assert login_shell_configs == ["allow_login_shell=false"]
    external_reasoning_configs = [
        value
        for index, value in enumerate(root_call)
        if index > 0
        and root_call[index - 1] == "--config"
        and value.startswith("mcp_servers.reasoning_")
    ]
    assert len(external_reasoning_configs) == 3
    for raw in external_reasoning_configs:
        server = tomllib.loads("value=" + raw.split("=", 1)[1])["value"]
        assert server["env"]["RETHLAS_BOUND_EXTERNAL_PLAN_PATH"] == str(
            plan_path.resolve()
        )
        assert server["env"]["RETHLAS_BOUND_EXTERNAL_PLAN_SHA256"] == (
            plan_sha256
        )
        assert server["env"][
            "RETHLAS_BOUND_EXTERNAL_PLAN_ROOT_SESSION_ID"
        ] == root_session_id
    assert "--skip-git-repo-check" in root_call
    root_configs = [
        value
        for index, value in enumerate(root_call)
        if index > 0 and root_call[index - 1] == "--config"
    ]
    if sys.platform == "darwin":
        assert "--sandbox" not in root_call
        assert 'default_permissions="axiom-relay-cohort"' in root_configs
        assert (
            'permissions.axiom-relay-cohort.network.enabled=false'
            in root_configs
        )
    else:
        assert root_call[root_call.index("--sandbox") + 1] == "workspace-write"
    probe = json.loads(isolation_probe.read_text(encoding="utf-8"))
    assert probe == {
        "claude_history": False,
        "codex_auth_write_succeeded": True,
        "codex_history": False,
        "codex_models_cache": False,
        "current_candidate_projection": True,
        "current_memory": True,
        "current_plan": True,
        "current_problem": True,
        "current_results": True,
        "git_history": False,
        "host_claude_state": False,
        "old_log": False,
        "other_memory": False,
        "other_plan": False,
        "other_problem_candidate_projection": False,
        "other_problem": False,
        "other_results": False,
        "other_statement_candidate_projection": False,
        "root_home_is_private_tmpfs": sys.platform.startswith("linux"),
    }
    assert host_auth.read_text(encoding="utf-8") == "host auth sentinel"
    if expected_mode == "matlas_arxiv":
        assert "search_matlas_theorems, search_arxiv_theorems, and " in prompt
        assert "read_arxiv_primary" in prompt
        assert "General web search, browser access" in prompt
        assert "at most two targeted queries" in prompt


@pytest.mark.parametrize(
    ("extra", "diagnostic"),
    [
        (
            {
                "RETHLAS_MAIN_AGENT": "opus",
                "RETHLAS_CLAUDE_BIN": "/definitely/missing/claude",
            },
            "Claude CLI must resolve",
        ),
        (
            {"RETHLAS_MAIN_AGENT": "unknown"},
            "must be gpt-sol, opus, fable, opus-sol-council, or prompt",
        ),
    ],
)
def test_invalid_or_unavailable_main_agent_starts_zero_models(
    tmp_path: Path,
    extra: dict[str, str],
    diagnostic: str,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    calls_file = tmp_path / "invalid-main-agent-calls.jsonl"
    environment = _mock_environment(
        runner,
        fake_bin,
        mode="forged",
        extra_environment={
            "RETHLAS_RUN_MODE": "core",
            "MOCK_CODEX_CALLS_FILE": str(calls_file),
            **extra,
        },
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )

    assert completed.returncode != 0
    assert diagnostic in completed.stderr
    if calls_file.exists():
        calls = [json.loads(line) for line in calls_file.read_text().splitlines()]
        assert not [call for call in calls if "exec" in call]


def test_axiom_relay_public_settings_dispatch_core(tmp_path: Path) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    environment = _mock_environment(
        runner,
        fake_bin,
        mode="trusted",
        extra_environment={
            "AXIOM_RELAY_RUN_MODE": "core",
            "AXIOM_RELAY_MAIN_AGENT": "gpt-sol",
            "AXIOM_RELAY_MODEL_POLICY_PROFILE": "compatible",
        },
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "Mode:       core" in completed.stdout
    assert "Main agent: gpt-sol" in completed.stdout


def test_axiom_relay_setting_conflict_fails_before_dispatch(tmp_path: Path) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    calls_file = tmp_path / "conflicting-brand-settings.jsonl"
    environment = _mock_environment(
        runner,
        fake_bin,
        mode="trusted",
        extra_environment={
            "AXIOM_RELAY_RUN_MODE": "core",
            "RETHLAS_RUN_MODE": "reviewed",
            "MOCK_CODEX_CALLS_FILE": str(calls_file),
        },
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )

    assert completed.returncode == 1
    assert "AXIOM_RELAY_RUN_MODE conflicts" in completed.stderr
    assert not calls_file.exists()


def test_legacy_alias_warns_and_dispatches_core(tmp_path: Path) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    environment = _mock_environment(
        runner,
        fake_bin,
        mode="trusted",
        extra_environment={"RETHLAS_RUN_MODE": "legacy"},
    )
    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "AXIOM_RELAY_RUN_MODE=legacy is deprecated; use core" in completed.stderr
    assert "Mode:       core" in completed.stdout


def test_isolated_legacy_runner_works_without_hotjoin_control_sources(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    generation_root = runner.parents[1]
    for path in (
        runner.with_name("run_hotjoin.sh"),
        generation_root / "guardian.py",
        generation_root / "guardian_launcher.py",
        generation_root.parent / "advisor_bridge.py",
    ):
        path.unlink()
    legacy_source = runner.with_name("run_legacy.sh").read_text(encoding="utf-8")
    for forbidden in (
        "hotjoin_adapter.py",
        "guardian.py",
        "guardian_launcher.py",
        "advisor_bridge.py",
        "review_client.py",
        "server_driver.py",
        "review.contracts",
        "review.critic",
    ):
        assert forbidden not in legacy_source
    environment = _mock_environment(
        runner,
        fake_bin,
        mode="trusted",
        extra_environment={
            "LEGACY_GENERATION_CONTROL_TOKEN": "ambient-legacy-token",
            "RETHLAS_GENERATION_CONTROL_TOKEN": "ambient-control-token",
        },
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    config = json.loads(
        (generation_root / "reasoning_mcp_config_seen.json").read_text(
            encoding="utf-8"
        )
    )
    assert config["env"]["RETHLAS_RUNTIME_PROFILE"] == "legacy"
    assert config["args"][4::3] == list(LEGACY_TRUSTED_MCP_LOGICAL_MODULES)
    assert "LEGACY_GENERATION_CONTROL_TOKEN" not in config["env"]
    assert "RETHLAS_GENERATION_CONTROL_TOKEN" not in config["env"]
    snapshot_mcp = Path(config["args"][-2]).parent
    assert {path.name for path in snapshot_mcp.iterdir()} == {
        "__init__.py",
        "publication_proof_context_v3.py",
        "proof_context.py",
        "legacy_verification_client.py",
        "legacy_server.py",
    }
    assert not (snapshot_mcp.parent / "review").exists()
    assert (snapshot_mcp.parent / "AGENTS.legacy.md").is_file()
    assert not (snapshot_mcp.parent / "AGENTS.md").exists()
    instruction_receipt = json.loads(
        (generation_root / "legacy_instructions_seen.json").read_text(
            encoding="utf-8"
        )
    )
    expected_instructions = (generation_root / "AGENTS.legacy.md").read_bytes()
    assert instruction_receipt == {
        "bytes": len(expected_instructions),
        "sha256": hashlib.sha256(expected_instructions).hexdigest(),
    }


def test_owned_legacy_runner_fd_is_snapshotted_from_any_shared_offset(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    legacy_runner = runner.with_name("run_legacy.sh").resolve()
    environment = _mock_environment(
        runner,
        fake_bin,
        mode="trusted",
        extra_environment={
            "RETHLAS_RUN_MODE": "core",
            "RETHLAS_MAIN_AGENT": "gpt-sol",
        },
    )
    runner_descriptor = os.open(legacy_runner, os.O_RDONLY)
    try:
        # /dev/fd duplicates the shared open-file description on Darwin.  The
        # production snapshot must therefore ignore an inherited EOF offset.
        os.lseek(runner_descriptor, 0, os.SEEK_END)
        environment.update(
            {
                "RETHLAS_OWNED_EXECUTABLE_ORIGIN": str(legacy_runner),
                "RETHLAS_OWNED_EXECUTABLE_FD": str(runner_descriptor),
                "RETHLAS_OWNED_EXECUTABLE_SHA256": hashlib.sha256(
                    legacy_runner.read_bytes()
                ).hexdigest(),
            }
        )
        completed = subprocess.run(
            [str(legacy_runner)],
            cwd=legacy_runner.parent.parent,
            env=environment,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
            pass_fds=(runner_descriptor,),
        )
    finally:
        os.close(runner_descriptor)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "fcopyfile failed" not in completed.stderr
    config = json.loads(
        (legacy_runner.parent.parent / "reasoning_mcp_config_seen.json").read_text(
            encoding="utf-8"
        )
    )
    snapshot_runner = (
        Path(config["args"][-2]).parent.parent / "tests" / "run_legacy.sh"
    )
    assert snapshot_runner.read_bytes() == legacy_runner.read_bytes()
    assert stat.S_IMODE(snapshot_runner.stat().st_mode) == (
        stat.S_IMODE(legacy_runner.stat().st_mode) & ~0o222
    )


def test_legacy_verifier_unavailable_starts_zero_paid_roots(tmp_path: Path) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    calls_file = tmp_path / "codex-calls.jsonl"
    environment = _mock_environment(
        runner,
        fake_bin,
        mode="forged",
        extra_environment={"MOCK_CODEX_CALLS_FILE": str(calls_file)},
    )
    environment.pop("RETHLAS_LEGACY_ALLOW_OFFLINE_DRAFT")

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 1
    assert "Refusing to start a paid Legacy root" in completed.stderr
    calls = [json.loads(line) for line in calls_file.read_text().splitlines()]
    assert not [call for call in calls if "exec" in call]


def test_legacy_binds_subagents_to_selected_root_model_and_effort(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    calls_file = tmp_path / "codex-calls.jsonl"
    environment = _mock_environment(
        runner,
        fake_bin,
        mode="trusted",
        extra_environment={
            "MODEL": "gpt-5.6-luna",
            "REASONING_EFFORT": "high",
            "MOCK_CODEX_CALLS_FILE": str(calls_file),
        },
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    calls = [json.loads(line) for line in calls_file.read_text().splitlines()]
    call = next(value for value in calls if "exec" in value)
    configs = [
        call[index + 1]
        for index, value in enumerate(call[:-1])
        if value == "--config"
    ]
    assert 'agents.default_subagent_model="gpt-5.6-luna"' in configs
    assert 'agents.default_subagent_reasoning_effort="high"' in configs


def test_max_diversity_launches_astra_max_for_root_and_subagents(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    calls_file = tmp_path / "codex-calls.jsonl"
    environment = _mock_environment(
        runner,
        fake_bin,
        mode="trusted",
        extra_environment={
            "AXIOM_RELAY_MODEL_POLICY_PROFILE": "max_diversity",
            "MOCK_CODEX_CALLS_FILE": str(calls_file),
        },
    )
    environment.pop("MODEL", None)
    environment.pop("REASONING_EFFORT", None)
    completed = subprocess.run(
        [str(runner)], cwd=runner.parent.parent, env=environment,
        text=True, capture_output=True, timeout=30, check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    calls = [json.loads(line) for line in calls_file.read_text().splitlines()]
    call = next(value for value in calls if "exec" in value)
    assert call[call.index("-m") + 1] == "gpt-6-astra"
    configs = [
        call[index + 1] for index, value in enumerate(call[:-1])
        if value == "--config"
    ]
    assert 'model_reasoning_effort="max"' in configs
    assert 'agents.default_subagent_model="gpt-6-astra"' in configs
    assert 'agents.default_subagent_reasoning_effort="max"' in configs


def test_legacy_rejects_invalid_offline_draft_selection_before_codex(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    calls_file = tmp_path / "codex-calls.jsonl"
    environment = _mock_environment(
        runner,
        fake_bin,
        mode="forged",
        extra_environment={
            "MOCK_CODEX_CALLS_FILE": str(calls_file),
            "RETHLAS_LEGACY_ALLOW_OFFLINE_DRAFT": "yes",
        },
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 1
    assert "must be 0 or 1" in completed.stderr
    assert not calls_file.exists()


def test_generated_legacy_server_is_current_and_control_free() -> None:
    builder = GENERATION_ROOT / "mcp" / "build_legacy_server.py"
    generated = GENERATION_ROOT / "mcp" / "legacy_server.py"
    generated_verification = (
        GENERATION_ROOT / "mcp" / "legacy_verification_client.py"
    )
    checked = subprocess.run(
        [sys.executable, "-B", str(builder), "--check"],
        cwd=GENERATION_ROOT,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )
    assert checked.returncode == 0, checked.stdout + checked.stderr

    tree = ast.parse(generated.read_text(encoding="utf-8"))
    definitions = {
        item.name
        for item in ast.walk(tree)
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }
    assert not definitions & {
        "advisor_report_get",
        "continuous_round_finish",
        "continuous_round_status",
        "context_handoff_get",
        "context_handoff_prepare",
        "context_handoff_status",
        "generation_yield",
        "review_frontier_status",
        "route_cycle_close",
        "route_review_close",
        "route_review_prepare",
        "route_review_status",
        "route_review_wait",
        "verify_review_claim",
    }
    loaded_names = {
        item.id
        for item in ast.walk(tree)
        if isinstance(item, ast.Name) and isinstance(item.ctx, ast.Load)
    }
    assert not {name for name in loaded_names if name.startswith("_adapter_")}
    imported_modules = {
        alias.name
        for item in ast.walk(tree)
        if isinstance(item, ast.Import)
        for alias in item.names
    } | {
        item.module or ""
        for item in ast.walk(tree)
        if isinstance(item, ast.ImportFrom)
    }
    assert not {
        name
        for name in imported_modules
        if name.startswith(("review", "advisor_client", "review_client"))
    }

    verification_text = generated_verification.read_text(encoding="utf-8")
    verification_tree = ast.parse(verification_text)
    verification_definitions = {
        item.name
        for item in ast.walk(verification_tree)
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }
    assert not verification_definitions & {
        "_parse_targeted_manifest",
        "validate_targeted_claim_receipt",
        "verify_targeted_claim_service",
    }
    lowered = verification_text.casefold()
    for forbidden in ("targeted_claim", "targeted verifier", "review_id", "route_id"):
        assert forbidden not in lowered

    proof_context_text = (GENERATION_ROOT / "mcp" / "proof_context.py").read_text(
        encoding="utf-8"
    ).casefold()
    for forbidden in (
        "targeted_claim",
        "review_id",
        "route_review",
        "generation_yield",
        "continuous_round_finish",
        "continuous_round_status",
    ):
        assert forbidden not in proof_context_text


def test_claude_runtime_dependency_manifest_matches_workspace() -> None:
    core_path = GENERATION_ROOT.parent / "claude_core.py"
    core_tree = ast.parse(core_path.read_text(encoding="utf-8"))
    manifest_node = next(
        node.value
        for node in core_tree.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name)
            and target.id == "RUNTIME_DEPENDENCY_SHA256"
            for target in node.targets
        )
    )
    manifest = ast.literal_eval(manifest_node)
    assert isinstance(manifest, dict)

    expected = {
        relative: hashlib.sha256(
            (GENERATION_ROOT / relative).read_bytes()
        ).hexdigest()
        for relative in manifest
    }
    assert manifest == expected


def test_generated_legacy_verification_client_has_errno_runtime_dependency() -> None:
    script = """
import errno
import importlib.util
import pathlib

path = pathlib.Path('mcp/legacy_verification_client.py').resolve(strict=True)
spec = importlib.util.spec_from_file_location('legacy_verification_client', path)
assert spec is not None and spec.loader is not None
client = importlib.util.module_from_spec(spec)
spec.loader.exec_module(client)

client.ctypes.CDLL = lambda *args, **kwargs: object()
try:
    client._renameat2_at(-1, 'source', 'destination', 0)
except OSError as exc:
    assert exc.errno == errno.ENOSYS
else:
    raise AssertionError('missing renameat2 must fail with ENOSYS')
"""
    environment = {
        **os.environ,
        "PYTHONDONTWRITEBYTECODE": "1",
        "RETHLAS_RUNTIME_PROFILE": "legacy",
    }
    checked = subprocess.run(
        [sys.executable, "-B", "-c", script],
        cwd=GENERATION_ROOT,
        env=environment,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )
    assert checked.returncode == 0, checked.stdout + checked.stderr


def test_legacy_replaced_source_commitments_bind_exact_source(
    tmp_path: Path,
) -> None:
    builder = GENERATION_ROOT / "mcp" / "build_legacy_server.py"
    source = GENERATION_ROOT / "mcp" / "server.py"
    changed = tmp_path / "server.py"
    source_text = source.read_text(encoding="utf-8")
    needle = "    checkpoint_dir = _batch_checkpoint_dir(problem_id)\n"
    assert source_text.count(needle) >= 1
    changed.write_text(
        source_text.replace(
            needle,
            "    # Replacement contract review probe.\n" + needle,
            1,
        ),
        encoding="utf-8",
    )

    checked = subprocess.run(
        [
            sys.executable,
            "-B",
            str(builder),
            "--check",
            "--source",
            str(changed),
        ],
        cwd=GENERATION_ROOT,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )

    assert checked.returncode != 0
    assert (
        "Legacy replacement requires review after source change" in checked.stderr
    )


def test_legacy_trusted_python_inline_calls_are_isolated() -> None:
    runner = (GENERATION_ROOT / "tests" / "run_legacy.sh").read_text(
        encoding="utf-8"
    )

    assert '"$TRUSTED_PYTHON_BIN" -B' not in runner


def test_legacy_generator_rejects_control_code_in_transitive_proof_context(
    tmp_path: Path,
) -> None:
    builder = GENERATION_ROOT / "mcp" / "build_legacy_server.py"
    source = GENERATION_ROOT / "mcp" / "proof_context.py"
    contaminated = tmp_path / "proof_context.py"
    contaminated.write_text(
        source.read_text(encoding="utf-8")
        + "\n\ndef generation_yield(*args, **kwargs):\n    return None\n",
        encoding="utf-8",
    )

    checked = subprocess.run(
        [
            sys.executable,
            "-B",
            str(builder),
            "--check",
            "--proof-context-source",
            str(contaminated),
        ],
        cwd=GENERATION_ROOT,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )

    assert checked.returncode != 0
    assert "Legacy proof-context isolation audit failed" in checked.stderr


def test_legacy_mcp_profile_exposes_no_continuous_or_owner_tools(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    environment = _mock_environment(runner, fake_bin, mode="forged")
    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 1
    generation_root = runner.parents[1]
    config = json.loads(
        (generation_root / "reasoning_mcp_config_seen.json").read_text(
            encoding="utf-8"
        )
    )
    mcp_python = _real_mcp_python()
    probe = _mcp_stdio_probe(
        [str(mcp_python), *config["args"]],
        cwd=Path(config["cwd"]),
        generation_root=generation_root,
        python_executable=mcp_python,
        extra_env=config["env"],
    )
    assert probe.returncode == 0, probe.stderr
    responses = [json.loads(line) for line in probe.stdout.splitlines() if line]
    tools_response = next(item for item in responses if item.get("id") == 2)
    tool_names = {item["name"] for item in tools_response["result"]["tools"]}
    assert tool_names == {
        "search_matlas_theorems",
        "search_arxiv_theorems",
        "read_arxiv_primary",
        "append_route_terminal_report",
        "verify_blueprint_service",
        "memory_init",
        "memory_append",
        "memory_append_batch",
        "memory_search",
        "branch_update",
    }
    leaked_home = tmp_path / "leaked-home"
    leaked_home.mkdir()
    leaked_environment = {
        "HOME": str(leaked_home),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": f"{mcp_python.parent}:/usr/bin:/bin",
        **config["env"],
        "RETHLAS_REVIEW_DB": str(tmp_path / "forbidden.sqlite3"),
    }
    leaked = subprocess.run(
        [str(mcp_python), *config["args"]],
        cwd=config["cwd"],
        env=leaked_environment,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )
    assert leaked.returncode != 0
    assert "legacy MCP server received continuous control bindings" in (
        leaked.stderr
    )


def test_prompted_hotjoin_rejects_bad_or_missing_run_id_before_codex(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    codex_calls = tmp_path / "mode-prompt-codex-calls.jsonl"
    environment = _mock_environment(
        runner,
        fake_bin,
        mode="forged",
        extra_environment={
            "MOCK_CODEX_CALLS_FILE": str(codex_calls),
            "RETHLAS_RUN_MODE": "prompt",
        },
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        input="2\n1\nbad run id\n",
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )

    assert completed.returncode == 1
    assert "Requires an explicit run id" in completed.stderr
    assert "Hot-join run id is invalid" in completed.stderr
    assert "ended before a value was provided" in completed.stderr
    assert not codex_calls.exists()


@pytest.mark.parametrize(
    ("mode", "run_id", "diagnostic"),
    [
        ("unknown", None, "must be core, reviewed, legacy, hotjoin, hot-join, or prompt"),
        ("reviewed", None, "requires AXIOM_RELAY_REVIEW_RUN_ID"),
        ("hotjoin", None, "requires AXIOM_RELAY_REVIEW_RUN_ID"),
        ("hotjoin", "bad run id", "must match"),
        ("core", "unexpected-run", "conflicts with AXIOM_RELAY_REVIEW_RUN_ID"),
        ("legacy", "unexpected-run", "conflicts with AXIOM_RELAY_REVIEW_RUN_ID"),
    ],
)
def test_explicit_mode_errors_start_zero_codex_processes(
    tmp_path: Path,
    mode: str,
    run_id: str | None,
    diagnostic: str,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    codex_calls = tmp_path / "mode-error-codex-calls.jsonl"
    extra_environment = {
        "MOCK_CODEX_CALLS_FILE": str(codex_calls),
        "RETHLAS_RUN_MODE": mode,
    }
    if run_id is not None:
        extra_environment["RETHLAS_HOTJOIN_RUN_ID"] = run_id
    environment = _mock_environment(
        runner,
        fake_bin,
        mode="forged",
        extra_environment=extra_environment,
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )

    assert completed.returncode == 1
    assert diagnostic in completed.stderr
    assert not codex_calls.exists()


def test_continuous_hotjoin_selects_monitor_only_guardian_and_policy(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    _adapter, state_path, calls_path = _install_mock_cadence_adapter(tmp_path)
    environment = _cadence_environment(
        runner,
        fake_bin,
        state_path,
        calls_path,
        dispositions=["hard_stopped"],
        max_iterations=1,
        extra_environment={
            "RETHLAS_RUN_MODE": "hotjoin",
            "RETHLAS_REVIEW_CADENCE_POLICY": (
                "rethlas_continuous_supervisor_v1"
            ),
            "MOCK_EXPECT_REVIEW_POLICY": "rethlas_continuous_supervisor_v1",
        },
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 70, completed.stdout + completed.stderr
    assert "Mode:       reviewed" in completed.stdout
    assert "AXIOM_RELAY_RUN_MODE=hotjoin is deprecated; use reviewed" in (
        completed.stderr
    )
    run_calls = _cadence_calls(calls_path, "run-generator")
    assert len(run_calls) == 1
    arguments = run_calls[0]["argv"]
    assert arguments[arguments.index("--review-cadence-policy") + 1] == (
        "rethlas_continuous_supervisor_v1"
    )
    launcher_calls = [
        json.loads(line)
        for line in calls_path.with_name(
            "guardian-launcher-calls.jsonl"
        ).read_text(encoding="utf-8").splitlines()
    ]
    assert len(launcher_calls) == 1
    assert "--guardian-mode" in launcher_calls[0]["argv"]
    assert launcher_calls[0]["argv"][
        launcher_calls[0]["argv"].index("--guardian-mode") + 1
    ] == "monitor_only"


def test_continuous_verified_publication_closes_durable_supervisor(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    _adapter, state_path, calls_path = _install_mock_cadence_adapter(tmp_path)
    environment = _cadence_environment(
        runner,
        fake_bin,
        state_path,
        calls_path,
        dispositions=["hard_stopped"],
        max_iterations=1,
        extra_environment={
            "MOCK_PUBLICATION": "trusted",
            "RETHLAS_REVIEW_CADENCE_POLICY": (
                "rethlas_continuous_supervisor_v1"
            ),
            "MOCK_EXPECT_REVIEW_POLICY": "rethlas_continuous_supervisor_v1",
        },
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "Solved problem_id=example" in completed.stdout
    closes = _cadence_calls(calls_path, "cadence-close")
    assert len(closes) == 1
    payload = closes[0]["control_envelope"]["payload"]
    assert payload["operation"] == "verified_completion"
    assert payload["published_bytes"] == len(b"mock verified proof")
    assert json.loads(state_path.read_text(encoding="utf-8"))["disposition"] == (
        "completed"
    )


def test_continuous_completion_close_recovers_without_second_paid_root(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    _adapter, state_path, calls_path = _install_mock_cadence_adapter(tmp_path)
    environment = _cadence_environment(
        runner,
        fake_bin,
        state_path,
        calls_path,
        dispositions=["continuous_root_missing"],
        max_iterations=1,
        extra_environment={
            "MOCK_FAIL_VERIFIED_COMPLETION_CLOSE": "1",
            "MOCK_PUBLICATION": "trusted",
            "RETHLAS_REVIEW_CADENCE_POLICY": (
                "rethlas_continuous_supervisor_v1"
            ),
            "MOCK_EXPECT_REVIEW_POLICY": "rethlas_continuous_supervisor_v1",
        },
    )
    first = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert first.returncode == 70, first.stdout + first.stderr
    assert json.loads(state_path.read_text(encoding="utf-8"))["disposition"] == (
        "continuous_root_missing"
    )

    recovered_environment = dict(environment)
    recovered_environment.pop("MOCK_FAIL_VERIFIED_COMPLETION_CLOSE")
    recovered = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=recovered_environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert recovered.returncode == 0, recovered.stdout + recovered.stderr
    assert "during continuous completion recovery" in recovered.stdout
    assert len(_cadence_calls(calls_path, "run-generator")) == 1
    closes = _cadence_calls(calls_path, "cadence-close")
    assert len(closes) == 2
    assert all(
        call["control_envelope"]["payload"]["operation"]
        == "verified_completion"
        for call in closes
    )
    assert json.loads(state_path.read_text(encoding="utf-8"))["disposition"] == (
        "completed"
    )


def test_disabled_cost_policy_is_resolved_once_and_forwarded_to_guarded_worker(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    _adapter, state_path, calls_path = _install_mock_cadence_adapter(tmp_path)
    environment = _cadence_environment(
        runner,
        fake_bin,
        state_path,
        calls_path,
        dispositions=["hard_stopped"],
        max_iterations=1,
        extra_environment={
            "RETHLAS_REVIEW_CADENCE_POLICY": (
                "rethlas_continuous_supervisor_v1"
            ),
            "MOCK_EXPECT_REVIEW_POLICY": "rethlas_continuous_supervisor_v1",
            "RETHLAS_COST_GATE_POLICY": "disabled_by_owner",
        },
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 70, completed.stdout + completed.stderr
    run_calls = _cadence_calls(calls_path, "run-generator")
    assert len(run_calls) == 1
    call = run_calls[0]
    assert call["raw_cost_policy_present"] is False
    assert isinstance(call["resolved_cost_policy_sha256"], str)
    prompt = call["argv"][call["argv"].index("--prompt") + 1]
    assert "cost policy is disabled_by_owner" in prompt
    assert call["resolved_cost_policy_sha256"] in prompt


@pytest.mark.parametrize(
    ("review_disposition", "expected_paid_roots", "prompt_fragment"),
    [
        (
            "continuous_review_host_recovery",
            1,
            "Do not start a new paid turn",
        ),
        (
            "continuous_verdict_successor_required",
            2,
            "Apply only the immutable host-completed continuous review verdict",
        ),
        (
            "continuous_intent_successor_required",
            2,
            "one durable cause-bound continuous root intent",
        ),
    ],
)
def test_continuous_review_recovery_dispositions_survive_wrapper_restart(
    tmp_path: Path,
    review_disposition: str,
    expected_paid_roots: int,
    prompt_fragment: str,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    _adapter, state_path, calls_path = _install_mock_cadence_adapter(tmp_path)
    environment = _cadence_environment(
        runner,
        fake_bin,
        state_path,
        calls_path,
        dispositions=[review_disposition, "hard_stopped"],
        max_iterations=1,
        extra_environment={
            "RETHLAS_REVIEW_CADENCE_POLICY": (
                "rethlas_continuous_supervisor_v1"
            ),
            "MOCK_EXPECT_REVIEW_POLICY": "rethlas_continuous_supervisor_v1",
        },
    )

    first = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert first.returncode == 70, first.stdout + first.stderr
    first_state = json.loads(state_path.read_text(encoding="utf-8"))
    assert first_state["disposition"] == review_disposition, (
        first.stdout + first.stderr
    )

    second = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert second.returncode == 70, second.stdout + second.stderr
    run_calls = _cadence_calls(calls_path, "run-generator")
    assert len(run_calls) == 2
    second_prompt = run_calls[1]["argv"][run_calls[1]["argv"].index("--prompt") + 1]
    assert prompt_fragment in second_prompt
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["paid_root_count"] == expected_paid_roots
    assert len(state["cycle_history"]) == 1


@pytest.mark.parametrize(
    ("guardian_mode", "diagnostic"),
    [
        ("false", "guardian enforcement is not released"),
        ("missing", "guardian_enforcement_ready must be an immutable boolean"),
        ("non_boolean", "guardian_enforcement_ready must be an immutable boolean"),
    ],
)
def test_unreleased_or_malformed_guardian_policy_starts_zero_control_or_paid_work(
    tmp_path: Path,
    guardian_mode: str,
    diagnostic: str,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    _adapter, state_path, calls_path = _install_mock_cadence_adapter(tmp_path)
    codex_calls = tmp_path / "guardian-hold-codex-calls.jsonl"
    environment = _cadence_environment(
        runner,
        fake_bin,
        state_path,
        calls_path,
        dispositions=["hard_stopped"],
        max_iterations=1,
        extra_environment={
            "MOCK_CODEX_CALLS_FILE": str(codex_calls),
            "MOCK_GUARDIAN_ENFORCEMENT_READY_MODE": guardian_mode,
            # An inherited wrapper value is not an authority and cannot
            # override the immutable host policy object.
            "RETHLAS_GUARDIAN_ENFORCEMENT_READY": "true",
        },
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 70, completed.stdout + completed.stderr
    assert diagnostic in completed.stderr
    calls = [
        json.loads(line) for line in calls_path.read_text(encoding="utf-8").splitlines()
    ]
    assert [call["command"] for call in calls] == ["policy-contract"]
    assert not state_path.exists()
    assert not codex_calls.exists()


def test_unreleased_guardian_nonfresh_resume_dry_run_reports_migration_with_zero_codex(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    adapter, state_path, calls_path = _install_mock_cadence_adapter(tmp_path)
    source_db, copy_db = _make_nonfresh_ledger_copy(runner, tmp_path)
    source_wal = Path(str(source_db) + "-wal")
    source_shm = Path(str(source_db) + "-shm")
    source_wal.write_bytes(b"")
    source_shm.write_bytes(b"\0" * 32768)
    source_wal.chmod(0o600)
    source_shm.chmod(0o600)
    codex_calls = tmp_path / "nonfresh-dry-run-codex-calls.jsonl"
    environment = _cadence_environment(
        runner,
        fake_bin,
        state_path,
        calls_path,
        dispositions=["hard_stopped"],
        max_iterations=1,
        extra_environment={
            "MOCK_CODEX_CALLS_FILE": str(codex_calls),
            "MOCK_GUARDIAN_ENFORCEMENT_READY_MODE": "false",
            "RETHLAS_NONFRESH_RESUME_DRY_RUN": "1",
            "RETHLAS_NONFRESH_RESUME_DB_COPY": str(copy_db),
        },
    )
    _seed_mock_cadence_projection(
        adapter,
        state_path,
        environment,
        disposition="stale_active",
    )
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["generation"] = 2
    state["thread_epoch"]["active_turn_id"] = "turn_mock_stale"
    state_path.write_text(
        json.dumps(state, allow_nan=False, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    calls_path.write_text("", encoding="utf-8")
    source_before = hashlib.sha256(source_db.read_bytes()).hexdigest()

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    report = json.loads(completed.stdout)
    assert report["schema_version"] == "rethlas_nonfresh_resume_dry_run_v1"
    assert report["diagnostic"] == "copied_legacy_ledger_nonfresh_resume"
    assert report["policy"]["guardian_enforcement_ready"] is False
    assert report["observed"] == {
        "active_turn_id": "turn_mock_stale",
        "cadence_disposition": "stale_active",
        "generation": 2,
        "paid_turn_allowed": False,
        "quarantine": None,
        "thread_id": "thread_mock_1",
    }
    assert report["decision"]["requested_topology"] == "reuse_existing_thread"
    assert report["decision"]["existing_thread_preserved"] is True
    assert report["decision"]["fresh_thread_forced_by_dry_run"] is False
    assert report["decision"]["resume_admitted"] is False
    assert report["decision"]["paid_processes_started"] is False
    assert (
        report["decision"]["recovery_migration_disposition"]
        == "legacy_stale_active_offline_reconciliation_required"
    )
    assert report["source_db"]["sha256_before"] == source_before
    assert report["source_db"]["sha256_after"] == source_before
    assert report["source_db"]["unchanged"] is True
    assert report["copy_db"]["schema_or_scheduler_migrated"] is False
    assert hashlib.sha256(source_db.read_bytes()).hexdigest() == source_before
    assert source_wal.read_bytes() == b""
    assert source_shm.stat().st_size == 32768
    calls = [
        json.loads(line) for line in calls_path.read_text(encoding="utf-8").splitlines()
    ]
    assert [call["command"] for call in calls] == [
        "policy-contract",
        "status",
        "cadence-control-state",
    ]
    assert all(str(copy_db) in call["argv"] for call in calls[1:])
    assert not _cadence_calls(calls_path, "init")
    assert not _cadence_calls(calls_path, "control-capability-bind")
    assert not _cadence_calls(calls_path, "review-drive")
    assert not _cadence_calls(calls_path, "guarded-review-drive")
    assert not _cadence_calls(calls_path, "run-generator")
    assert not codex_calls.exists()
    assert not (runner.parents[2] / ".trusted_generation_runtime").exists()
    assert not (runner.parents[2] / ".verification_receipts").exists()
    assert not (runner.parents[2] / ".rethlas_advisor").exists()
    assert "no Codex, reviewer, recovery, or paid control action" in completed.stderr
    assert "schema projection mutation was confined to the copy" in completed.stderr
    assert "resume_admitted" not in completed.stderr


def test_nonfresh_resume_dry_run_rejects_source_inode_alias_before_any_codex(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    _adapter, state_path, calls_path = _install_mock_cadence_adapter(tmp_path)
    source_db, _copy_db = _make_nonfresh_ledger_copy(runner, tmp_path)
    codex_calls = tmp_path / "nonfresh-alias-codex-calls.jsonl"
    environment = _cadence_environment(
        runner,
        fake_bin,
        state_path,
        calls_path,
        dispositions=["hard_stopped"],
        max_iterations=1,
        extra_environment={
            "MOCK_CODEX_CALLS_FILE": str(codex_calls),
            "MOCK_GUARDIAN_ENFORCEMENT_READY_MODE": "false",
            "RETHLAS_NONFRESH_RESUME_DRY_RUN": "1",
            "RETHLAS_NONFRESH_RESUME_DB_COPY": str(source_db),
        },
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 70, completed.stdout + completed.stderr
    assert "copied-ledger DB copy aliases the source DB inode" in completed.stderr
    assert not calls_path.exists()
    assert not state_path.exists()
    assert not codex_calls.exists()


def test_nonfresh_resume_dry_run_rejects_active_source_sidecar_before_adapter(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    _adapter, state_path, calls_path = _install_mock_cadence_adapter(tmp_path)
    source_db, copy_db = _make_nonfresh_ledger_copy(runner, tmp_path)
    source_wal = Path(str(source_db) + "-wal")
    source_wal.write_bytes(b"active source sentinel")
    source_wal.chmod(0o600)
    codex_calls = tmp_path / "nonfresh-sidecar-codex-calls.jsonl"
    environment = _cadence_environment(
        runner,
        fake_bin,
        state_path,
        calls_path,
        dispositions=["hard_stopped"],
        max_iterations=1,
        extra_environment={
            "MOCK_CODEX_CALLS_FILE": str(codex_calls),
            "MOCK_GUARDIAN_ENFORCEMENT_READY_MODE": "false",
            "RETHLAS_NONFRESH_RESUME_DRY_RUN": "1",
            "RETHLAS_NONFRESH_RESUME_DB_COPY": str(copy_db),
        },
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 70, completed.stdout + completed.stderr
    assert "source DB has a non-empty SQLite WAL" in completed.stderr
    assert not calls_path.exists()
    assert not state_path.exists()
    assert not codex_calls.exists()


def test_unreleased_guardian_stale_reconcile_is_zero_model_and_never_resumes(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    adapter, state_path, calls_path = _install_mock_cadence_adapter(tmp_path)
    source_db, copy_db = _make_nonfresh_ledger_copy(runner, tmp_path)
    source_wal = Path(str(source_db) + "-wal")
    source_shm = Path(str(source_db) + "-shm")
    source_wal.write_bytes(b"")
    source_shm.write_bytes(b"\0" * 32768)
    source_wal.chmod(0o600)
    source_shm.chmod(0o600)
    codex_calls = tmp_path / "stale-reconcile-codex-calls.jsonl"
    environment = _cadence_environment(
        runner,
        fake_bin,
        state_path,
        calls_path,
        dispositions=["hard_stopped"],
        max_iterations=1,
        extra_environment={
            "MOCK_CODEX_CALLS_FILE": str(codex_calls),
            "MOCK_GUARDIAN_ENFORCEMENT_READY_MODE": "false",
            "RETHLAS_NONFRESH_STALE_RECONCILE": "1",
            "RETHLAS_NONFRESH_RESUME_DB_COPY": str(copy_db),
            "RETHLAS_NONFRESH_EXPECTED_THREAD_ID": "thread_mock_1",
            "RETHLAS_NONFRESH_EXPECTED_TURN_ID": "turn_mock_stale",
        },
    )
    _seed_mock_cadence_projection(
        adapter,
        state_path,
        environment,
        disposition="stale_active",
    )
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["generation"] = 2
    state["thread_epoch"]["active_turn_id"] = "turn_mock_stale"
    state_path.write_text(
        json.dumps(state, allow_nan=False, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    calls_path.write_text("", encoding="utf-8")
    source_before = hashlib.sha256(source_db.read_bytes()).hexdigest()

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 70, completed.stdout + completed.stderr
    assert completed.stdout, completed.stderr
    report = json.loads(completed.stdout)
    assert report["schema_version"] == "rethlas_nonfresh_stale_reconcile_report_v1"
    assert report["run_id"] == "mock-cadence-live"
    assert report["thread_id"] == "thread_mock_1"
    assert report["turn_id"] == "turn_mock_stale"
    assert report["initial_disposition"] == "stale_active"
    assert report["post_disposition"] == "operational_blocked"
    assert report["reconcile_result"]["state"] == ("terminal_reconciled_quarantined")
    assert report["handoff_candidate"] == {
        "eligible": True,
        "resume_authority": False,
        "source_terminal_sha256": "3" * 64,
        "source_thread_read_response_sha256": "1" * 64,
        "use": (
            "host_may_extract_one_bounded_handoff_candidate_from_quarantined_thread_read"
        ),
    }
    assert report["decision"] == {
        "fresh_thread_started": False,
        "model_calls_started": 0,
        "next_action": (
            "host_may_extract_one_bounded_handoff_candidate_from_quarantined_thread_read"
        ),
        "paid_turns_started": 0,
        "read_only_app_server_calls": ["initialize", "thread/read"],
        "read_only_app_server_processes_started": 1,
        "resume_admitted": False,
    }
    assert report["source_db"]["sha256_before"] == source_before
    assert report["source_db"]["sha256_after"] == source_before
    assert hashlib.sha256(source_db.read_bytes()).hexdigest() == source_before
    assert source_wal.read_bytes() == b""
    assert source_shm.stat().st_size == 32768
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["disposition"] == "operational_blocked"
    assert state["thread_epoch"]["active_turn_id"] is None
    assert state["quarantine"]["kind"] == "adapter_loss_terminal_discontinuity"
    commands = [
        json.loads(line)["command"]
        for line in calls_path.read_text(encoding="utf-8").splitlines()
    ]
    assert commands == [
        "policy-contract",
        "stale-recovery-capability-prepare",
        "status",
        "cadence-control-state",
        "stale-turn-reconcile",
        "status",
        "cadence-control-state",
    ]
    assert "init" not in commands
    assert "control-capability-bind" not in commands
    assert "run-generator" not in commands
    assert "review-drive" not in commands
    assert "guarded-review-drive" not in commands
    assert not codex_calls.exists()
    assert "zero model/paid turns/reviewers/verifiers" in completed.stderr
    calls_text = calls_path.read_text(encoding="utf-8")
    assert "RETHLAS_STALE_RECOVERY_TOKEN" not in calls_text
    assert "RETHLAS_REVIEW_CONTROL_TOKEN" not in calls_text


def test_stale_reconcile_rejects_tampered_capability_receipt_before_app_server(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    adapter, state_path, calls_path = _install_mock_cadence_adapter(tmp_path)
    source_db, copy_db = _make_nonfresh_ledger_copy(runner, tmp_path)
    codex_calls = tmp_path / "stale-tamper-codex-calls.jsonl"
    environment = _cadence_environment(
        runner,
        fake_bin,
        state_path,
        calls_path,
        dispositions=["hard_stopped"],
        max_iterations=1,
        extra_environment={
            "MOCK_CODEX_CALLS_FILE": str(codex_calls),
            "MOCK_GUARDIAN_ENFORCEMENT_READY_MODE": "false",
            "MOCK_TAMPER_STALE_PREPARE_RECEIPT": "1",
            "RETHLAS_NONFRESH_STALE_RECONCILE": "1",
            "RETHLAS_NONFRESH_RESUME_DB_COPY": str(copy_db),
            "RETHLAS_NONFRESH_EXPECTED_THREAD_ID": "thread_mock_1",
            "RETHLAS_NONFRESH_EXPECTED_TURN_ID": "turn_mock_stale",
        },
    )
    _seed_mock_cadence_projection(
        adapter,
        state_path,
        environment,
        disposition="stale_active",
    )
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["generation"] = 2
    state["thread_epoch"]["active_turn_id"] = "turn_mock_stale"
    state_path.write_text(
        json.dumps(state, allow_nan=False, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    calls_path.write_text("", encoding="utf-8")
    source_before = hashlib.sha256(source_db.read_bytes()).hexdigest()

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 70, completed.stdout + completed.stderr
    assert "receipt SHA-256 mismatch" in completed.stderr
    commands = [
        json.loads(line)["command"]
        for line in calls_path.read_text(encoding="utf-8").splitlines()
    ]
    assert commands == [
        "policy-contract",
        "stale-recovery-capability-prepare",
    ]
    assert hashlib.sha256(source_db.read_bytes()).hexdigest() == source_before
    assert "stale-turn-reconcile" not in commands
    assert "run-generator" not in commands
    assert not codex_calls.exists()


def test_guardian_release_policy_digest_tamper_starts_zero_control_or_paid_work(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    _adapter, state_path, calls_path = _install_mock_cadence_adapter(tmp_path)
    codex_calls = tmp_path / "guardian-tamper-codex-calls.jsonl"
    environment = _cadence_environment(
        runner,
        fake_bin,
        state_path,
        calls_path,
        dispositions=["hard_stopped"],
        max_iterations=1,
        extra_environment={
            "MOCK_CODEX_CALLS_FILE": str(codex_calls),
            "MOCK_TAMPER_GUARDIAN_POLICY_DIGEST": "1",
        },
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 70, completed.stdout + completed.stderr
    assert "review policy_sha256 mismatch" in completed.stderr
    calls = [
        json.loads(line) for line in calls_path.read_text(encoding="utf-8").splitlines()
    ]
    assert [call["command"] for call in calls] == ["policy-contract"]
    assert not state_path.exists()
    assert not codex_calls.exists()


def test_cadence_rejects_non_sixty_minute_prompt_clock_before_codex(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    codex_calls = tmp_path / "codex-calls.jsonl"
    environment = _mock_environment(
        runner,
        fake_bin,
        mode="forged",
        extra_environment={
            "MOCK_CODEX_CALLS_FILE": str(codex_calls),
            "RETHLAS_DEEP_WORK_MINUTES": "90",
            "RETHLAS_HOTJOIN_RUN_ID": "mock-cadence-live",
        },
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )

    assert completed.returncode == 1
    assert "must be 60 under a durable hot-join policy" in completed.stderr
    assert not codex_calls.exists()


def test_cadence_rejects_non_owner_running_receipt_before_paid_turn(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    generation_server = runner.parent.parent / "mcp" / "server.py"
    source = generation_server.read_text(encoding="utf-8")
    trusted_reason = 'reason="owner_runner_started",'
    assert source.count(trusted_reason) == 1
    generation_server.write_text(
        source.replace(
            trusted_reason,
            'reason="untrusted_running_reason",',
            1,
        ),
        encoding="utf-8",
    )
    _adapter, state_path, calls_path = _install_mock_cadence_adapter(tmp_path)
    environment = _cadence_environment(
        runner,
        fake_bin,
        state_path,
        calls_path,
        dispositions=["hard_stopped"],
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 70, completed.stdout + completed.stderr
    assert not _cadence_calls(calls_path, "run-generator")
    assert "running control reason is not owner_runner_started" in completed.stderr


def test_cadence_legal_generation_yield_stops_before_another_paid_cycle(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    _adapter, state_path, calls_path = _install_mock_cadence_adapter(tmp_path)
    environment = _cadence_environment(
        runner,
        fake_bin,
        state_path,
        calls_path,
        dispositions=["hard_stopped_unfinalized"],
        extra_environment={"MOCK_HOTJOIN_LEGAL_YIELD": "1"},
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert len(_cadence_calls(calls_path, "run-generator")) == 1
    assert "state=waiting_cost_gate" in completed.stdout
    assert "owner action is required before another paid turn" in completed.stdout


def test_guardian_predispatch_failure_preserves_original_error_before_cycle_check(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    _adapter, state_path, calls_path = _install_mock_cadence_adapter(tmp_path)
    environment = _cadence_environment(
        runner,
        fake_bin,
        state_path,
        calls_path,
        dispositions=["hard_stopped"],
        extra_environment={"MOCK_GUARDIAN_LAUNCHER_FAIL_BEFORE_DISPATCH": "1"},
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 70, completed.stdout + completed.stderr
    assert "distinct authenticated cycle_id" not in completed.stderr
    assert "generator exited with code 70" in completed.stderr
    assert not _cadence_calls(calls_path, "run-generator")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["cycle_id"] is None
    guarded_logs = list(
        (runner.parents[2] / ".rethlas_hotjoin" / "logs").rglob("*_iter_0.md")
    )
    assert len(guarded_logs) == 1
    assert "mock guardian pre-dispatch failure" in guarded_logs[0].read_text(
        encoding="utf-8"
    )


def test_clean_early_terminal_gets_one_same_cycle_authorization_without_reset(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    _adapter, state_path, calls_path = _install_mock_cadence_adapter(tmp_path)
    environment = _cadence_environment(
        runner,
        fake_bin,
        state_path,
        calls_path,
        dispositions=["continuation_authorization_required", "hard_stopped"],
        max_iterations=1,
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 1, completed.stdout + completed.stderr
    run_calls = _cadence_calls(calls_path, "run-generator")
    assert len(run_calls) == 2
    admits = _cadence_calls(calls_path, "cadence-admit")
    assert [call["control_envelope"]["payload"]["operation"] for call in admits] == [
        "continue_active_cycle"
    ]
    all_calls = [
        json.loads(line) for line in calls_path.read_text(encoding="utf-8").splitlines()
    ]
    command_order = [call["command"] for call in all_calls]
    second_run_index = [
        index
        for index, command in enumerate(command_order)
        if command == "run-generator"
    ][1]
    assert (
        command_order.index("control-capability-bind")
        < command_order.index("cadence-admit")
        < second_run_index
    )
    second_arguments = run_calls[1]["argv"]
    second_prompt = second_arguments[second_arguments.index("--prompt") + 1]
    assert "existing app-server thread epoch" in second_prompt
    assert "original absolute cycle T0" in second_prompt
    assert "T+60m/T+120m cooperative review drains" in second_prompt
    assert "not a new cycle or a clock reset" in second_prompt
    assert "brand-new app-server thread epoch" not in second_prompt
    _assert_cadence_capabilities_are_fd_only(calls_path, state_path)


def test_reviewer_red_without_generation_yield_freezes_before_any_root_continuation(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    _adapter, state_path, calls_path = _install_mock_cadence_adapter(tmp_path)
    environment = _cadence_environment(
        runner,
        fake_bin,
        state_path,
        calls_path,
        dispositions=["continuation_authorization_required", "hard_stopped"],
        max_iterations=1,
        extra_environment={"MOCK_CADENCE_ALLOWED_ACTION": "freeze_route"},
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 70, completed.stdout + completed.stderr
    assert len(_cadence_calls(calls_path, "run-generator")) == 1
    assert not _cadence_calls(calls_path, "cadence-close")
    assert not _cadence_calls(calls_path, "cadence-admit")
    assert "owner_wait" not in (completed.stdout + completed.stderr)
    assert "allowed action" in completed.stderr


def test_same_cycle_short_turns_are_not_truncated_by_max_iterations(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    _adapter, state_path, calls_path = _install_mock_cadence_adapter(tmp_path)
    environment = _cadence_environment(
        runner,
        fake_bin,
        state_path,
        calls_path,
        dispositions=["continuation_authorization_required"] * 11 + ["hard_stopped"],
        max_iterations=1,
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=90,
        check=False,
    )

    assert completed.returncode == 1, completed.stdout + completed.stderr
    assert len(_cadence_calls(calls_path, "run-generator")) == 12
    assert len(_cadence_calls(calls_path, "cadence-admit")) == 11
    assert "finalized T+150m hard stop" in completed.stdout
    assert "Owner cycle budget" not in completed.stderr


def _set_runner_paid_root_failsafe(runner: Path, value: int) -> None:
    hotjoin_runner = runner.with_name("run_hotjoin.sh")
    source = hotjoin_runner.read_text(encoding="utf-8")
    needle = "CADENCE_ROOT_INVOCATION_FAILSAFE=128"
    assert source.count(needle) == 1
    hotjoin_runner.write_text(
        source.replace(needle, f"CADENCE_ROOT_INVOCATION_FAILSAFE={value}"),
        encoding="utf-8",
    )


def test_paid_root_failsafe_resets_only_for_distinct_continue_next_cycle(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    _set_runner_paid_root_failsafe(runner, 3)
    _adapter, state_path, calls_path = _install_mock_cadence_adapter(tmp_path)
    environment = _cadence_environment(
        runner,
        fake_bin,
        state_path,
        calls_path,
        dispositions=[
            "continue_active_cycle",
            "continue_active_cycle",
            "continue_next_cycle",
            "continue_active_cycle",
            "continue_active_cycle",
            "hard_stopped",
        ],
        max_iterations=2,
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=45,
        check=False,
    )

    assert completed.returncode == 1, completed.stdout + completed.stderr
    assert len(_cadence_calls(calls_path, "run-generator")) == 6
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert len(state["cycle_history"]) == 2
    assert len(set(state["cycle_history"])) == 2
    assert "3-paid-root operational fail-safe" not in completed.stderr


def test_paid_root_failsafe_does_not_reset_inside_one_cycle(tmp_path: Path) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    _set_runner_paid_root_failsafe(runner, 3)
    _adapter, state_path, calls_path = _install_mock_cadence_adapter(tmp_path)
    environment = _cadence_environment(
        runner,
        fake_bin,
        state_path,
        calls_path,
        dispositions=["continue_active_cycle"] * 4,
        max_iterations=1,
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 70, completed.stdout + completed.stderr
    assert len(_cadence_calls(calls_path, "run-generator")) == 3
    assert "3-paid-root operational fail-safe" in completed.stderr
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert len(state["cycle_history"]) == 1


def test_due_review_starts_no_ordinary_full_capability_continuation(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    _adapter, state_path, calls_path = _install_mock_cadence_adapter(tmp_path)
    environment = _cadence_environment(
        runner,
        fake_bin,
        state_path,
        calls_path,
        dispositions=["review_turn_authorization_required", "hard_stopped"],
        max_iterations=1,
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 70, completed.stdout + completed.stderr
    run_calls = _cadence_calls(calls_path, "run-generator")
    assert len(run_calls) == 1
    assert not _cadence_calls(calls_path, "cadence-admit")
    assert "ordinary full-capability generator continuation" in completed.stderr
    assert "trusted host review orchestration" in completed.stderr


@pytest.mark.parametrize(
    "disposition",
    [
        "review_turn_authorization_required",
        "continue_review_only",
    ],
)
def test_due_review_wrapper_restart_starts_zero_root_turns_until_host_drive(
    tmp_path: Path,
    disposition: str,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    adapter, state_path, calls_path = _install_mock_cadence_adapter(tmp_path)
    environment = _cadence_environment(
        runner,
        fake_bin,
        state_path,
        calls_path,
        dispositions=["hard_stopped"],
        max_iterations=1,
    )
    _seed_mock_cadence_projection(
        adapter,
        state_path,
        environment,
        disposition=disposition,
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 70, completed.stdout + completed.stderr
    assert not _cadence_calls(calls_path, "run-generator")
    assert not _cadence_calls(calls_path, "control-capability-bind")
    assert not _cadence_calls(calls_path, "cadence-admit")
    assert "ordinary full-capability generator turn is forbidden" in completed.stderr
    assert "No root model turn was started" in completed.stderr


def test_due_review_uses_guarded_runner_then_starts_fresh_epoch(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    adapter, state_path, calls_path = _install_mock_cadence_adapter(tmp_path)
    environment = _cadence_environment(
        runner,
        fake_bin,
        state_path,
        calls_path,
        dispositions=["hard_stopped"],
        max_iterations=1,
        extra_environment={
            "MOCK_GUARDIAN_ENFORCE_CURRENT_CAPABILITY_REVISION": "1"
        },
    )
    _seed_mock_cadence_projection(
        adapter,
        state_path,
        environment,
        disposition="review_drive_required",
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 1, completed.stdout + completed.stderr
    assert len(_cadence_calls(calls_path, "control-capability-bind")) == 2
    drives = _cadence_calls(calls_path, "guarded-review-drive")
    assert len(drives) == 1
    assert drives[0]["control_envelope"] is None
    drive_arguments = drives[0]["argv"]
    assert drive_arguments[drive_arguments.index("--run-id") + 1] == (
        "mock-cadence-live"
    )
    assert drive_arguments[drive_arguments.index("--boundary-id") + 1] == (
        "reviewbound_" + "b" * 32
    )
    run_calls = _cadence_calls(calls_path, "run-generator")
    assert len(run_calls) == 1
    prompt = run_calls[0]["argv"][run_calls[0]["argv"].index("--prompt") + 1]
    assert prompt.startswith("[TRUSTED HOST REHYDRATION REQUIRED]")
    all_calls = [
        json.loads(line) for line in calls_path.read_text(encoding="utf-8").splitlines()
    ]
    command_order = [call["command"] for call in all_calls]
    assert command_order.index("guarded-review-drive") < command_order.index(
        "run-generator"
    )
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["capability_revision"] == 2
    assert len(state["token_digests"]) == 2
    assert state["review_drive_count"] == 1
    assert state["reviewed_handoff_consumed_count"] == 1
    assert state["cycle_history"] == ["cycle_" + f"{1:032x}"]
    assert state["disposition"] == "hard_stopped"
    _assert_guarded_review_drive_is_fd_only(calls_path, state_path)
    assert "same-cycle fresh epoch is ready" in completed.stderr


def test_reviewed_epoch_early_terminal_gets_same_epoch_continuation(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    adapter, state_path, calls_path = _install_mock_cadence_adapter(tmp_path)
    environment = _cadence_environment(
        runner,
        fake_bin,
        state_path,
        calls_path,
        dispositions=[
            "continuation_authorization_required",
            "continuation_authorization_required",
            "hard_stopped",
        ],
        max_iterations=1,
        extra_environment={
            "MOCK_REVIEWED_ALLOWED_ACTION": "one_bounded_cycle_on_fatal_doubt"
        },
    )
    _seed_mock_cadence_projection(
        adapter,
        state_path,
        environment,
        disposition="review_drive_required",
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 1, completed.stdout + completed.stderr
    assert len(_cadence_calls(calls_path, "guarded-review-drive")) == 1
    assert len(_cadence_calls(calls_path, "run-generator")) == 2
    admits = _cadence_calls(calls_path, "cadence-admit")
    assert [call["control_envelope"]["payload"]["operation"] for call in admits] == [
        "continue_active_cycle"
    ]
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["reviewed_handoff_consumed_count"] == 1
    assert state["thread_epoch"]["thread_epoch"] == 2
    assert state["thread_epoch"]["handoff_id"].startswith("handoff_")
    assert state["disposition"] == "hard_stopped"


def test_due_review_red_freezes_route_without_owner_yield_or_paid_root(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    adapter, state_path, calls_path = _install_mock_cadence_adapter(tmp_path)
    environment = _cadence_environment(
        runner,
        fake_bin,
        state_path,
        calls_path,
        dispositions=["hard_stopped"],
        max_iterations=1,
        extra_environment={"MOCK_REVIEW_DRIVE_RED": "1"},
    )
    _seed_mock_cadence_projection(
        adapter,
        state_path,
        environment,
        disposition="review_drive_required",
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 1, completed.stdout + completed.stderr
    assert len(_cadence_calls(calls_path, "guarded-review-drive")) == 1
    assert not _cadence_calls(calls_path, "run-generator")
    assert not _cadence_calls(calls_path, "cadence-admit")
    assert not _cadence_calls(calls_path, "cadence-close")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["disposition"] == "route_frozen"
    assert state["allowed_action"] == "recovery_only"
    _assert_guarded_review_drive_is_fd_only(calls_path, state_path)
    assert "owner_wait" not in (completed.stdout + completed.stderr)
    assert "official review froze the active route after red" in completed.stderr
    assert "no authorized fallback" in completed.stderr

    restarted = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert restarted.returncode == 1, restarted.stdout + restarted.stderr
    assert len(_cadence_calls(calls_path, "control-capability-bind")) == 2
    assert len(_cadence_calls(calls_path, "guarded-review-drive")) == 1
    assert not _cadence_calls(calls_path, "run-generator")
    assert "state=route_frozen" in restarted.stderr
    assert "authorizes no additional paid work" in restarted.stderr


def test_initial_route_frozen_is_normal_unsolved_terminal_with_zero_paid_work(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    adapter, state_path, calls_path = _install_mock_cadence_adapter(tmp_path)
    environment = _cadence_environment(
        runner,
        fake_bin,
        state_path,
        calls_path,
        dispositions=["hard_stopped"],
        max_iterations=1,
    )
    _seed_mock_cadence_projection(
        adapter,
        state_path,
        environment,
        disposition="route_frozen",
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 1, completed.stdout + completed.stderr
    assert not _cadence_calls(calls_path, "control-capability-bind")
    assert not _cadence_calls(calls_path, "review-drive")
    assert not _cadence_calls(calls_path, "guarded-review-drive")
    assert not _cadence_calls(calls_path, "run-generator")
    assert not _cadence_calls(calls_path, "cadence-admit")
    assert not _cadence_calls(calls_path, "cadence-close")
    assert "official red verdict with no authorized fallback" in completed.stderr
    assert "not an owner/advisor wait" in completed.stderr


def test_post_turn_route_frozen_stops_normally_before_any_new_paid_turn(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    _adapter, state_path, calls_path = _install_mock_cadence_adapter(tmp_path)
    environment = _cadence_environment(
        runner,
        fake_bin,
        state_path,
        calls_path,
        dispositions=["continue_active_cycle"],
        max_iterations=1,
        extra_environment={"MOCK_POST_TURN_ROUTE_FROZEN": "1"},
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 1, completed.stdout + completed.stderr
    assert len(_cadence_calls(calls_path, "run-generator")) == 1
    assert not _cadence_calls(calls_path, "cadence-admit")
    assert not _cadence_calls(calls_path, "cadence-close")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["disposition"] == "route_frozen"
    assert state["allowed_action"] == "freeze_route"
    assert (
        "no owner/advisor wait or paid continuation is authorized" in completed.stderr
    )


def test_post_turn_stop_unsolved_stops_before_any_paid_successor(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    _adapter, state_path, calls_path = _install_mock_cadence_adapter(tmp_path)
    environment = _cadence_environment(
        runner,
        fake_bin,
        state_path,
        calls_path,
        dispositions=["continue_active_cycle"],
        max_iterations=1,
        extra_environment={
            "RETHLAS_REVIEW_CADENCE_POLICY": (
                "rethlas_continuous_supervisor_v1"
            ),
            "MOCK_EXPECT_REVIEW_POLICY": "rethlas_continuous_supervisor_v1",
            "MOCK_POST_TURN_STOP_UNSOLVED": "1",
        },
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 1, completed.stdout + completed.stderr
    assert len(_cadence_calls(calls_path, "run-generator")) == 1
    assert not _cadence_calls(calls_path, "cadence-admit")
    assert not _cadence_calls(calls_path, "cadence-close")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["disposition"] == "stop_unsolved"
    assert state["allowed_action"] == "recovery_only"
    assert "no paid successor is authorized" in completed.stderr


def test_initial_continuous_stop_unsolved_starts_zero_paid_work(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    adapter, state_path, calls_path = _install_mock_cadence_adapter(tmp_path)
    environment = _cadence_environment(
        runner,
        fake_bin,
        state_path,
        calls_path,
        dispositions=["hard_stopped"],
        max_iterations=1,
        extra_environment={
            "RETHLAS_REVIEW_CADENCE_POLICY": (
                "rethlas_continuous_supervisor_v1"
            ),
            "MOCK_EXPECT_REVIEW_POLICY": "rethlas_continuous_supervisor_v1",
        },
    )
    _seed_mock_cadence_projection(
        adapter,
        state_path,
        environment,
        disposition="stop_unsolved",
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 1, completed.stdout + completed.stderr
    assert not _cadence_calls(calls_path, "run-generator")
    assert not _cadence_calls(calls_path, "cadence-admit")
    assert not _cadence_calls(calls_path, "cadence-close")
    assert "already at durable stop_unsolved" in completed.stderr
    assert "no paid successor is authorized" in completed.stderr


def test_post_review_handoff_gate_restarts_zero_root_or_reviewer_processes(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    adapter, state_path, calls_path = _install_mock_cadence_adapter(tmp_path)
    environment = _cadence_environment(
        runner,
        fake_bin,
        state_path,
        calls_path,
        dispositions=["hard_stopped"],
        max_iterations=1,
    )
    _seed_mock_cadence_projection(
        adapter,
        state_path,
        environment,
        disposition="post_review_handoff_required",
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 70, completed.stdout + completed.stderr
    assert not _cadence_calls(calls_path, "run-generator")
    assert not _cadence_calls(calls_path, "control-capability-bind")
    assert not _cadence_calls(calls_path, "review-drive")
    assert not _cadence_calls(calls_path, "guarded-review-drive")
    assert "host-prepared fresh-epoch handoff is not yet available" in completed.stderr


def test_reviewed_epoch_restart_requires_existing_guardian_settle_without_second_root(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    adapter, state_path, calls_path = _install_mock_cadence_adapter(tmp_path)
    environment = _cadence_environment(
        runner,
        fake_bin,
        state_path,
        calls_path,
        dispositions=["hard_stopped"],
        max_iterations=1,
        extra_environment={"MOCK_FAIL_AFTER_REVIEWED_EPOCH_DISPATCH": "1"},
    )
    _seed_mock_cadence_projection(
        adapter,
        state_path,
        environment,
        disposition="continue_reviewed_cycle_fresh_epoch",
    )
    state = json.loads(state_path.read_text(encoding="utf-8"))
    handoff_sha256 = "9" * 64
    state["thread_epoch"] = {
        "active_turn_id": None,
        "handoff_id": f"handoff_{handoff_sha256}",
        "handoff_sha256": handoff_sha256,
        "predecessor_epoch": 1,
        "state": "pending",
        "thread_epoch": 2,
        "thread_id": None,
    }
    state_path.write_text(
        json.dumps(state, allow_nan=False, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )

    first = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    # The external launcher normalizes every nonzero worker terminal to the
    # fail-closed host code; the durable disposition remains the source of the
    # more specific recovery state.
    assert first.returncode == 70, first.stdout + first.stderr
    failed_state = json.loads(state_path.read_text(encoding="utf-8"))
    assert failed_state["disposition"] == "terminal_observed_pending_finalization"
    assert failed_state["paid_root_count"] == 1

    restarted_environment = dict(environment)
    restarted_environment.pop("MOCK_FAIL_AFTER_REVIEWED_EPOCH_DISPATCH")
    second = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=restarted_environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert second.returncode == 70, second.stdout + second.stderr
    run_calls = _cadence_calls(calls_path, "run-generator")
    assert len(run_calls) == 1
    assert "prior root terminal is still settling under its existing Guardian" in (
        second.stderr
    )
    assert "refusing capability rotation or a second root" in second.stderr
    final_state = json.loads(state_path.read_text(encoding="utf-8"))
    assert final_state["paid_root_count"] == 1
    assert final_state["disposition"] == "terminal_observed_pending_finalization"
    assert final_state["capability_revision"] == failed_state["capability_revision"]
    assert final_state["token_digests"] == failed_state["token_digests"]
    assert final_state["generation_control_instances"] == failed_state[
        "generation_control_instances"
    ]


def test_review_boundary_recovery_reaps_only_existing_root_and_descendants(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    adapter, state_path, calls_path = _install_mock_cadence_adapter(tmp_path)
    environment = _cadence_environment(
        runner,
        fake_bin,
        state_path,
        calls_path,
        dispositions=["hard_stopped"],
        max_iterations=1,
        extra_environment={
            "MOCK_GUARDIAN_ENFORCE_CURRENT_CAPABILITY_REVISION": "1"
        },
    )
    _seed_mock_cadence_projection(
        adapter,
        state_path,
        environment,
        disposition="review_boundary_recovery_required",
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 1, completed.stdout + completed.stderr
    run_calls = _cadence_calls(calls_path, "run-generator")
    assert len(run_calls) == 2
    prompt = run_calls[0]["argv"][run_calls[0]["argv"].index("--prompt") + 1]
    assert "Recover only the already-authorized durable scheduler operation" in prompt
    assert "Do not start a new paid turn" in prompt
    fresh_prompt = run_calls[1]["argv"][run_calls[1]["argv"].index("--prompt") + 1]
    assert fresh_prompt.startswith("[TRUSTED HOST REHYDRATION REQUIRED]")
    all_calls = [
        json.loads(line) for line in calls_path.read_text(encoding="utf-8").splitlines()
    ]
    command_order = [call["command"] for call in all_calls]
    recovery_index, fresh_index = [
        index
        for index, command in enumerate(command_order)
        if command == "run-generator"
    ]
    drive_index = command_order.index("guarded-review-drive")
    assert recovery_index < drive_index < fresh_index
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["run_count"] == 2
    assert state["review_drive_count"] == 1
    assert state["reviewed_handoff_consumed_count"] == 1
    assert state["disposition"] == "hard_stopped"
    assert len(_cadence_calls(calls_path, "guarded-review-drive")) == 1
    _assert_guarded_review_drive_is_fd_only(calls_path, state_path)
    assert "same-cycle fresh epoch is ready" in completed.stderr


@pytest.mark.parametrize(
    ("dispositions", "expiry_environment", "authorized_disposition"),
    [
        (
            ["continuation_authorization_required"],
            "MOCK_ACTIVE_AUTH_EXPIRED_AT_REVIEW_DUE",
            "continue_active_cycle",
        ),
    ],
)
def test_pre_rpc_cas_starts_zero_root_turns_under_expired_authorization(
    tmp_path: Path,
    dispositions: list[str],
    expiry_environment: str,
    authorized_disposition: str,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    _adapter, state_path, calls_path = _install_mock_cadence_adapter(tmp_path)
    environment = _cadence_environment(
        runner,
        fake_bin,
        state_path,
        calls_path,
        dispositions=dispositions,
        max_iterations=1,
        extra_environment={expiry_environment: "1"},
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 70, completed.stdout + completed.stderr
    assert len(_cadence_calls(calls_path, "run-generator")) == 2
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["run_count"] == 1
    assert state["disposition"] != authorized_disposition


def test_active_cycle_authorization_survives_true_wrapper_restart_with_rotation(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    _adapter, state_path, calls_path = _install_mock_cadence_adapter(tmp_path)
    environment = _cadence_environment(
        runner,
        fake_bin,
        state_path,
        calls_path,
        dispositions=["continuation_authorization_required", "hard_stopped"],
        extra_environment={"MOCK_FAIL_WRAPPER_AFTER_ACTIVE_ADMIT": "1"},
    )

    first = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert first.returncode == 70, first.stdout + first.stderr
    assert len(_cadence_calls(calls_path, "run-generator")) == 1
    first_state = json.loads(state_path.read_text(encoding="utf-8"))
    assert first_state["disposition"] == "continue_active_cycle"

    restarted_environment = dict(environment)
    restarted_environment.pop("MOCK_FAIL_WRAPPER_AFTER_ACTIVE_ADMIT")
    second = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=restarted_environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert second.returncode == 1, second.stdout + second.stderr
    assert len(_cadence_calls(calls_path, "run-generator")) == 2
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert len(set(state["token_digests"])) == 2
    assert len(set(state["helper_paths"])) == 2
    assert len(set(state["review_driver_paths"])) == 2
    assert len(set(state["generation_control_instances"])) == 2
    assert len(set(state["helper_digests"])) == 1
    assert len(set(state["review_driver_digests"])) == 1
    assert len(set(state["review_driver_package_digests"])) == 1
    assert len(set(state["runtime_digests"])) == 1
    assert len(set(state["codex_digests"])) == 1


@pytest.mark.parametrize(
    ("yield_state", "owner_wait"),
    [
        ("waiting_cost_gate", "owner_wait_cost"),
        ("waiting_owner_advisor_decision", "owner_wait_advisor"),
    ],
)
def test_authenticated_owner_yield_closes_and_resumes_on_fresh_epoch(
    tmp_path: Path,
    yield_state: str,
    owner_wait: str,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    _adapter, state_path, calls_path = _install_mock_cadence_adapter(tmp_path)
    environment = _cadence_environment(
        runner,
        fake_bin,
        state_path,
        calls_path,
        dispositions=["hard_stopped_unfinalized", "hard_stopped"],
        extra_environment={"MOCK_HOTJOIN_LEGAL_YIELD": yield_state},
    )

    first = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert first.returncode == 0, first.stdout + first.stderr
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["disposition"] == owner_wait
    closes = _cadence_calls(calls_path, "cadence-close")
    assert closes[-1]["control_envelope"]["payload"]["operation"] == "owner_yield"

    restarted_environment = dict(environment)
    restarted_environment.pop("MOCK_HOTJOIN_LEGAL_YIELD")
    second = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=restarted_environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert second.returncode == 1, second.stdout + second.stderr
    run_calls = _cadence_calls(calls_path, "run-generator")
    assert len(run_calls) == 2
    second_arguments = run_calls[1]["argv"]
    second_prompt = second_arguments[second_arguments.index("--prompt") + 1]
    assert "brand-new app-server thread epoch" in second_prompt
    admits = _cadence_calls(calls_path, "cadence-admit")
    assert admits[-1]["control_envelope"]["payload"]["operation"] == "owner_resume"


def test_owner_resume_crash_after_running_receipt_keeps_wait_until_next_invocation(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    _adapter, state_path, calls_path = _install_mock_cadence_adapter(tmp_path)
    environment = _cadence_environment(
        runner,
        fake_bin,
        state_path,
        calls_path,
        dispositions=["hard_stopped_unfinalized", "hard_stopped"],
        extra_environment={"MOCK_HOTJOIN_LEGAL_YIELD": "waiting_cost_gate"},
    )

    yielded = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert yielded.returncode == 0, yielded.stdout + yielded.stderr
    assert json.loads(state_path.read_text(encoding="utf-8"))["disposition"] == (
        "owner_wait_cost"
    )

    crashing_environment = dict(environment)
    crashing_environment.pop("MOCK_HOTJOIN_LEGAL_YIELD")
    crashing_environment["MOCK_FAIL_BEFORE_OWNER_RESUME_CAS"] = "1"
    crashed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=crashing_environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert crashed.returncode == 70, crashed.stdout + crashed.stderr
    state_after_crash = json.loads(state_path.read_text(encoding="utf-8"))
    assert state_after_crash["disposition"] == "owner_wait_cost"
    assert len(_cadence_calls(calls_path, "run-generator")) == 1
    failed_admit = _cadence_calls(calls_path, "cadence-admit")[-1]
    assert failed_admit["control_envelope"]["payload"]["operation"] == "owner_resume"
    assert (
        failed_admit["control_envelope"]["payload"]["generation_control_receipt"][
            "control"
        ]["instance_id"]
        == state_after_crash["generation_control_instances"][-1]
    )

    resumed_environment = dict(crashing_environment)
    resumed_environment.pop("MOCK_FAIL_BEFORE_OWNER_RESUME_CAS")
    resumed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=resumed_environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert resumed.returncode == 1, resumed.stdout + resumed.stderr
    final_state = json.loads(state_path.read_text(encoding="utf-8"))
    assert len(_cadence_calls(calls_path, "run-generator")) == 2
    assert len(set(final_state["generation_control_instances"])) == 3
    successful_admit = _cadence_calls(calls_path, "cadence-admit")[-1]
    assert (
        successful_admit["control_envelope"]["payload"]["generation_control_receipt"][
            "control"
        ]["instance_id"]
        == final_state["generation_control_instances"][-1]
    )
    assert (
        successful_admit["control_envelope"]["payload"]["generation_control_receipt"][
            "control"
        ]["instance_id"]
        != failed_admit["control_envelope"]["payload"]["generation_control_receipt"][
            "control"
        ]["instance_id"]
    )


@pytest.mark.parametrize(
    "yield_state",
    ["waiting_cost_gate", "waiting_owner_advisor_decision"],
)
def test_restart_preserves_prior_owner_yield_until_host_recovery_is_available(
    tmp_path: Path,
    yield_state: str,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    _adapter, state_path, calls_path = _install_mock_cadence_adapter(tmp_path)
    environment = _cadence_environment(
        runner,
        fake_bin,
        state_path,
        calls_path,
        dispositions=["hard_stopped_unfinalized", "hard_stopped"],
        extra_environment={
            "MOCK_HOTJOIN_LEGAL_YIELD": yield_state,
            "MOCK_FAIL_AFTER_YIELD_BEFORE_CADENCE_CLOSE": "1",
        },
    )

    crashed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    # Guardian exposes one uniform fail-closed return code while the durable
    # owner-yield-close disposition preserves the exact interrupted operation.
    assert crashed.returncode == 70, crashed.stdout + crashed.stderr
    crashed_state = json.loads(state_path.read_text(encoding="utf-8"))
    assert crashed_state["disposition"] == "owner_yield_close_required"
    assert len(_cadence_calls(calls_path, "run-generator")) == 1
    assert not _cadence_calls(calls_path, "cadence-close")
    prior_instances = list(crashed_state["generation_control_instances"])
    prior_token_digests = list(crashed_state["token_digests"])

    restarted_environment = dict(environment)
    restarted_environment.pop("MOCK_HOTJOIN_LEGAL_YIELD")
    restarted_environment.pop("MOCK_FAIL_AFTER_YIELD_BEFORE_CADENCE_CLOSE")
    resumed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=restarted_environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert resumed.returncode == 70, resumed.stdout + resumed.stderr
    final_state = json.loads(state_path.read_text(encoding="utf-8"))
    assert final_state["disposition"] == "owner_yield_close_required"
    assert final_state["generation_control_instances"] == prior_instances
    assert final_state["token_digests"] == prior_token_digests
    assert len(_cadence_calls(calls_path, "run-generator")) == 1
    assert not _cadence_calls(calls_path, "cadence-close")
    assert "wait receipt will not be overwritten" in resumed.stderr


def test_owner_yield_without_exact_host_bound_handoff_fails_closed(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    _adapter, state_path, calls_path = _install_mock_cadence_adapter(tmp_path)
    environment = _cadence_environment(
        runner,
        fake_bin,
        state_path,
        calls_path,
        dispositions=["hard_stopped_unfinalized"],
        extra_environment={
            "MOCK_HOTJOIN_LEGAL_YIELD": "waiting_cost_gate",
            "MOCK_CORRUPT_OWNER_YIELD_HANDOFF": "1",
        },
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 70, completed.stdout + completed.stderr
    assert not _cadence_calls(calls_path, "cadence-close")
    assert "handoff binding is invalid" in completed.stderr


def test_continue_next_cycle_is_the_only_new_paid_cycle_disposition(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    _adapter, state_path, calls_path = _install_mock_cadence_adapter(tmp_path)
    environment = _cadence_environment(
        runner,
        fake_bin,
        state_path,
        calls_path,
        dispositions=["continue_next_cycle", "hard_stopped_unfinalized"],
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )

    assert completed.returncode == 70, completed.stdout + completed.stderr
    run_calls = _cadence_calls(calls_path, "run-generator")
    assert len(run_calls) == 2
    second_arguments = run_calls[1]["argv"]
    assert isinstance(second_arguments, list)
    second_prompt = second_arguments[second_arguments.index("--prompt") + 1]
    assert "brand-new app-server thread epoch" in second_prompt
    assert "authenticated context handoff" in second_prompt
    assert "own durable pre-dispatch T0" in second_prompt
    assert "new absolute review/close/hard-stop deadlines" in second_prompt
    assert "never resets or extends the already closed prior cycle" in second_prompt
    assert "unchanged T+150m hard stop" not in second_prompt
    assert "disposition=hard_stopped_unfinalized" in completed.stderr
    assert "RETHLAS_REVIEW_CONTROL_TOKEN" not in calls_path.read_text(encoding="utf-8")
    for run_call in run_calls:
        arguments = run_call["argv"]
        mcp = tomllib.loads(
            "value=" + arguments[arguments.index("--mcp-config-toml") + 1]
        )["value"]
        assert "RETHLAS_REVIEW_CONTROL_TOKEN" not in mcp["env"]
        assert all("MASTER" not in name for name in mcp["env"])
        assert len(arguments[arguments.index("--trusted-runtime-sha256") + 1]) == 64
    for log_path in Path(environment["LOG_DIR"]).glob("*.md"):
        assert "RETHLAS_REVIEW_CONTROL_TOKEN" not in log_path.read_text(
            encoding="utf-8"
        )


def test_continue_next_cycle_requires_authenticated_pending_fresh_epoch(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    _adapter, state_path, calls_path = _install_mock_cadence_adapter(tmp_path)
    environment = _cadence_environment(
        runner,
        fake_bin,
        state_path,
        calls_path,
        dispositions=["continue_next_cycle"],
        extra_environment={"MOCK_CORRUPT_CONTINUE_EPOCH": "1"},
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )

    assert completed.returncode == 70, completed.stdout + completed.stderr
    assert len(_cadence_calls(calls_path, "run-generator")) == 1
    assert "forbids a paid turn" in completed.stderr
    assert "no paid turn was started" in completed.stderr


@pytest.mark.parametrize(
    ("first_disposition", "expected_calls", "expected_text"),
    [
        ("continue_next_cycle", 2, "brand-new app-server thread epoch"),
        ("hard_stopped", 1, "finalized T+150m hard stop"),
    ],
)
def test_t90_continues_only_with_t87_validated_handoff(
    tmp_path: Path,
    first_disposition: str,
    expected_calls: int,
    expected_text: str,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    _adapter, state_path, calls_path = _install_mock_cadence_adapter(tmp_path)
    dispositions = (
        ["continue_next_cycle", "hard_stopped"]
        if first_disposition == "continue_next_cycle"
        else ["hard_stopped"]
    )
    environment = _cadence_environment(
        runner,
        fake_bin,
        state_path,
        calls_path,
        dispositions=dispositions,
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 1, completed.stdout + completed.stderr
    run_calls = _cadence_calls(calls_path, "run-generator")
    assert len(run_calls) == expected_calls
    combined = completed.stdout + completed.stderr
    if expected_calls == 2:
        second_arguments = run_calls[1]["argv"]
        second_prompt = second_arguments[second_arguments.index("--prompt") + 1]
        assert expected_text in second_prompt
    else:
        assert expected_text in combined


def test_offline_absolute_t90_preflight_starts_zero_root_or_reviewer_turns(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    _adapter, state_path, calls_path = _install_mock_cadence_adapter(tmp_path)
    reviewer_calls = tmp_path / "reviewer-calls.jsonl"
    environment = _cadence_environment(
        runner,
        fake_bin,
        state_path,
        calls_path,
        dispositions=["continuation_authorization_required"],
        extra_environment={
            "MOCK_ABSOLUTE_DEADLINE_EXPIRED": "1",
            "MOCK_REVIEWER_CALLS_FILE": str(reviewer_calls),
        },
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 70, completed.stdout + completed.stderr
    assert not _cadence_calls(calls_path, "run-generator")
    assert not _cadence_calls(calls_path, "control-capability-bind")
    assert not reviewer_calls.exists()
    assert "no model or recovery turn is authorized" in completed.stderr


def test_continue_wrapper_restart_rotates_token_and_snapshot_path_not_identity(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    _adapter, state_path, calls_path = _install_mock_cadence_adapter(tmp_path)
    environment = _cadence_environment(
        runner,
        fake_bin,
        state_path,
        calls_path,
        dispositions=["continue_next_cycle"],
        max_iterations=1,
    )

    first = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=40,
        check=False,
    )
    second = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=40,
        check=False,
    )

    assert first.returncode == 1, first.stdout + first.stderr
    assert second.returncode == 1, second.stdout + second.stderr
    assert len(_cadence_calls(calls_path, "run-generator")) == 2
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert len(set(state["token_digests"])) == 2
    assert len(set(state["helper_paths"])) == 2
    assert len(set(state["review_driver_paths"])) == 2
    assert len(set(state["generation_control_instances"])) == 2
    assert len(set(state["helper_digests"])) == 1
    assert len(set(state["review_driver_digests"])) == 1
    assert len(set(state["review_driver_package_digests"])) == 1
    assert len(set(state["runtime_digests"])) == 1
    assert len(set(state["codex_digests"])) == 1
    assert all("runtime." in path for path in state["helper_paths"])


def test_hard_stopped_unfinalized_wrapper_restart_starts_no_recovery_or_paid_turn(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    _adapter, state_path, calls_path = _install_mock_cadence_adapter(tmp_path)
    environment = _cadence_environment(
        runner,
        fake_bin,
        state_path,
        calls_path,
        dispositions=["hard_stopped_unfinalized"],
    )

    first = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )
    assert first.returncode == 70, first.stdout + first.stderr
    assert len(_cadence_calls(calls_path, "run-generator")) == 1

    second = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )
    assert second.returncode == 70, second.stdout + second.stderr
    assert len(_cadence_calls(calls_path, "run-generator")) == 1
    assert "no model or recovery turn is authorized" in second.stderr
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert len(state["token_digests"]) == 1


def test_pending_hard_stop_terminal_requires_existing_guardian_not_a_new_root(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    _adapter, state_path, calls_path = _install_mock_cadence_adapter(tmp_path)
    environment = _cadence_environment(
        runner,
        fake_bin,
        state_path,
        calls_path,
        dispositions=["terminal_observed_pending_finalization", "hard_stopped"],
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )

    assert completed.returncode == 70, completed.stdout + completed.stderr
    run_calls = _cadence_calls(calls_path, "run-generator")
    assert len(run_calls) == 1
    assert "a pending terminal must be finalized by its existing Guardian" in (
        completed.stderr
    )
    assert "Could not derive an exact Guardian admission" in completed.stderr
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["disposition"] == "terminal_observed_pending_finalization"
    assert state["paid_root_count"] == 1


def test_finalized_hard_stop_is_normal_unsolved_terminal_not_operational_error(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    _adapter, state_path, calls_path = _install_mock_cadence_adapter(tmp_path)
    environment = _cadence_environment(
        runner,
        fake_bin,
        state_path,
        calls_path,
        dispositions=["hard_stopped"],
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )

    assert completed.returncode == 1, completed.stdout + completed.stderr
    assert len(_cadence_calls(calls_path, "run-generator")) == 1
    assert "finalized T+150m hard stop" in completed.stdout
    assert "no additional paid cycle is authorized" in completed.stdout
    assert "state=hard_stopped" in completed.stderr
    assert "operational" not in (completed.stdout + completed.stderr).lower()

    restarted = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )
    assert restarted.returncode == 1, restarted.stdout + restarted.stderr
    assert len(_cadence_calls(calls_path, "run-generator")) == 1
    assert "already at its finalized T+150m hard stop" in restarted.stderr
    assert "No recovery or additional paid cycle is authorized" in restarted.stderr


def test_recovery_that_remains_pending_fails_closed_without_second_recovery(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    _adapter, state_path, calls_path = _install_mock_cadence_adapter(tmp_path)
    environment = _cadence_environment(
        runner,
        fake_bin,
        state_path,
        calls_path,
        dispositions=["terminal_observed_pending_finalization"],
        max_iterations=3,
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )

    assert completed.returncode == 70, completed.stdout + completed.stderr
    assert len(_cadence_calls(calls_path, "run-generator")) == 1
    assert "a pending terminal must be finalized by its existing Guardian" in (
        completed.stderr
    )
    assert "Could not derive an exact Guardian admission" in completed.stderr
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["disposition"] == "terminal_observed_pending_finalization"
    assert state["paid_root_count"] == 1


def test_cadence_source_mutation_fails_after_fingerprint_bound_invocation(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    _adapter, state_path, calls_path = _install_mock_cadence_adapter(tmp_path)
    environment = _cadence_environment(
        runner,
        fake_bin,
        state_path,
        calls_path,
        dispositions=["continue_next_cycle"],
        max_iterations=1,
        extra_environment={"MOCK_MUTATE_HOTJOIN_SOURCE": "1"},
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )

    assert completed.returncode == 70, completed.stdout + completed.stderr
    assert len(_cadence_calls(calls_path, "run-generator")) == 1
    assert (
        "Trusted Guardian/control/helper/Codex sources changed during iter=0"
        in completed.stderr
    )
    assert "refusing to continue" in completed.stderr


def test_review_helper_mutation_before_spawn_starts_zero_reviewer_and_root_turns(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    _adapter, state_path, calls_path = _install_mock_cadence_adapter(tmp_path)
    helper_source = tmp_path / "agents" / "review" / "contract_cli.py"
    reviewer_calls = tmp_path / "reviewer-calls.jsonl"
    environment = _cadence_environment(
        runner,
        fake_bin,
        state_path,
        calls_path,
        dispositions=["continue_next_cycle"],
        extra_environment={
            "MOCK_MUTATE_REVIEW_HELPER_DURING_PREFLIGHT": "1",
            "MOCK_REVIEW_HELPER_SOURCE": str(helper_source),
            "MOCK_REVIEWER_CALLS_FILE": str(reviewer_calls),
        },
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )

    assert completed.returncode == 70, completed.stdout + completed.stderr
    assert not _cadence_calls(calls_path, "run-generator")
    assert not reviewer_calls.exists()
    assert "Trusted control/helper/Codex sources changed" in completed.stderr


def test_review_driver_dependency_mutation_starts_zero_reviewer_and_root_turns(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    adapter, state_path, calls_path = _install_mock_cadence_adapter(tmp_path)
    driver_dependency = runner.parent.parent / "mcp" / "review_client.py"
    environment = _cadence_environment(
        runner,
        fake_bin,
        state_path,
        calls_path,
        dispositions=["hard_stopped"],
        extra_environment={
            "MOCK_MUTATE_REVIEW_DRIVER_PACKAGE_DURING_PREFLIGHT": "1",
            "MOCK_REVIEW_DRIVER_PACKAGE_SOURCE": str(driver_dependency),
        },
    )
    _seed_mock_cadence_projection(
        adapter,
        state_path,
        environment,
        disposition="review_drive_required",
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )

    assert completed.returncode == 70, completed.stdout + completed.stderr
    assert not _cadence_calls(calls_path, "control-capability-bind")
    assert not _cadence_calls(calls_path, "review-drive")
    assert not _cadence_calls(calls_path, "guarded-review-drive")
    assert not _cadence_calls(calls_path, "run-generator")
    assert "Trusted control/helper/Codex sources changed" in completed.stderr


def test_recursive_cost_policy_mutation_starts_zero_reviewer_and_root_turns(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    _adapter, state_path, calls_path = _install_mock_cadence_adapter(tmp_path)
    skill_source = (
        runner.parent.parent / ".agents" / "skills" / "recursive-proving" / "SKILL.md"
    )
    reviewer_calls = tmp_path / "reviewer-calls.jsonl"
    environment = _cadence_environment(
        runner,
        fake_bin,
        state_path,
        calls_path,
        dispositions=["continue_next_cycle"],
        extra_environment={
            "MOCK_MUTATE_RECURSIVE_SKILL_DURING_PREFLIGHT": "1",
            "MOCK_RECURSIVE_SKILL_SOURCE": str(skill_source),
            "MOCK_REVIEWER_CALLS_FILE": str(reviewer_calls),
        },
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 70, completed.stdout + completed.stderr
    assert not _cadence_calls(calls_path, "run-generator")
    assert not _cadence_calls(calls_path, "control-capability-bind")
    assert not reviewer_calls.exists()
    assert "Trusted control/helper/Codex sources changed" in completed.stderr


def test_codex_mutation_before_spawn_starts_zero_reviewer_and_root_turns(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    _adapter, state_path, calls_path = _install_mock_cadence_adapter(tmp_path)
    reviewer_calls = tmp_path / "reviewer-calls.jsonl"
    environment = _cadence_environment(
        runner,
        fake_bin,
        state_path,
        calls_path,
        dispositions=["continue_next_cycle"],
        extra_environment={
            "MOCK_MUTATE_CODEX_DURING_PREFLIGHT": "1",
            "MOCK_CODEX_SOURCE": str(fake_bin / "codex"),
            "MOCK_REVIEWER_CALLS_FILE": str(reviewer_calls),
        },
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 70, completed.stdout + completed.stderr
    assert not _cadence_calls(calls_path, "run-generator")
    assert not _cadence_calls(calls_path, "control-capability-bind")
    assert not reviewer_calls.exists()
    assert "Trusted control/helper/Codex sources changed" in completed.stderr


def test_group_writable_codex_binary_is_rejected_before_execution(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    codex_calls = tmp_path / "codex-calls.jsonl"
    codex_path = fake_bin / "codex"
    codex_path.chmod(0o775)
    environment = _mock_environment(
        runner,
        fake_bin,
        mode="forged",
        extra_environment={"MOCK_CODEX_CALLS_FILE": str(codex_calls)},
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 1, completed.stdout + completed.stderr
    assert "must not be group/world-writable" in completed.stderr
    assert not codex_calls.exists()


def test_runner_accepts_mock_atomic_publication_receipt(tmp_path: Path) -> None:
    completed = _run_mock(tmp_path, mode="trusted")
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "Mode:       core" in completed.stdout
    assert "Solved problem_id=example" in completed.stdout


def test_legacy_runner_projects_terminal_aggregate_token_usage(
    tmp_path: Path,
) -> None:
    completed = _run_mock(
        tmp_path,
        mode="trusted",
        extra_environment={"MOCK_CODEX_TOKEN_USAGE": "125980"},
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "Cohort aggregate tokens used\n125980\n" in completed.stdout


def test_legacy_runner_still_accepts_historical_v2_publication_receipt(
    tmp_path: Path,
) -> None:
    completed = _run_mock(tmp_path, mode="trusted_v2")
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "Mode:       core" in completed.stdout
    assert "Solved problem_id=example" in completed.stdout


def test_runner_prompts_enforce_safe_three_route_phase_sequence(tmp_path: Path) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    calls_file = tmp_path / "codex-calls.jsonl"
    environment = _mock_environment(
        runner,
        fake_bin,
        mode="forged",
        extra_environment={
            "MAX_ITERATIONS": "3",
            "RETHLAS_DEEP_WORK_MINUTES": "90",
            "RETHLAS_LEGACY_STOP_AFTER_CURRENT_COHORT": "1",
            "RETHLAS_GENERATION_PYTHON_BIN": str(fake_bin / "python3"),
            "MOCK_CODEX_CALLS_FILE": str(calls_file),
            "MOCK_FRONTIER_PROGRESS": "1",
        },
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )

    assert completed.returncode == 1
    calls = [json.loads(line) for line in calls_file.read_text().splitlines()]
    exec_calls = [call for call in calls if "exec" in call]
    assert len(exec_calls) == 3
    prompts = [call[-1] for call in exec_calls]
    assert all("--disable" in call and "hooks" in call for call in exec_calls)
    assert all("injected AGENTS.legacy.md developer profile" in p for p in prompts)
    assert "protected root route-design phase" in prompts[0]
    assert "90 minutes as a soft target" in prompts[0]
    assert "do not initialize or write memory" in prompts[0]
    assert "exactly three materially different, scope-disjoint routes" in prompts[0]
    assert "$legacy-three-route" in prompts[0]
    assert "one pre-fanout checkpoint" in prompts[0]
    assert "exact three context-free solvers" in prompts[0]
    assert "must not pursue a fourth proof route" in prompts[0]
    assert "scheduled review" not in prompts[0]
    assert "at most one bounded memory_search" in prompts[1]
    assert "Do not use arXiv theorem search or web search" in prompts[1]
    assert "capabilities, not obligations" in prompts[2]
    assert "one named external knowledge gap" in prompts[2]
    assert all("candidate fast lane" in prompt for prompt in prompts)
    assert all("do not call generation_yield" in prompt for prompt in prompts)
    assert all("stop-after-current-cohort gate" in prompt for prompt in prompts)
    assert all("Do not checkpoint or spawn another cohort" in prompt for prompt in prompts)
    assert "Cohort cap: stop after the current complete cohort" in completed.stdout
    assert f"Math Python: {fake_bin / 'python3'}" in completed.stdout

    web_modes = []
    for call in exec_calls:
        config_values = [
            call[index + 1]
            for index, value in enumerate(call[:-1])
            if value == "--config" and call[index + 1].startswith("web_search=")
        ]
        assert len(config_values) == 1
        web_modes.append(config_values[0])
    assert web_modes == [
        'web_search="disabled"',
        'web_search="disabled"',
        'web_search="live"',
    ]


@pytest.mark.parametrize(
    "waiting_state",
    ("waiting_cost_gate", "waiting_owner_advisor_decision"),
)
def test_isolated_legacy_rejects_owner_wait_without_second_paid_turn(
    tmp_path: Path, waiting_state: str
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    calls_file = tmp_path / "codex-calls.jsonl"
    environment = _mock_environment(
        runner,
        fake_bin,
        mode="forged",
        extra_environment={
            "MAX_ITERATIONS": "2",
            "MOCK_CODEX_CALLS_FILE": str(calls_file),
            "MOCK_GENERATION_CONTROL_STATE": waiting_state,
        },
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )

    assert completed.returncode == 70, completed.stdout + completed.stderr
    calls = [json.loads(line) for line in calls_file.read_text().splitlines()]
    assert len([call for call in calls if "exec" in call]) == 1
    assert not (Path(environment["LOG_DIR"]) / "example_iter_1.md").exists()
    assert "legacy generation control forbids owner-wait states" in completed.stderr
    assert "Solved problem_id=" not in completed.stdout


def test_runner_ordinary_unfinished_turn_stops_without_frontier_progress(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    calls_file = tmp_path / "codex-calls.jsonl"
    environment = _mock_environment(
        runner,
        fake_bin,
        mode="forged",
        extra_environment={
            "MAX_ITERATIONS": "2",
            "MOCK_CODEX_CALLS_FILE": str(calls_file),
        },
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )

    assert completed.returncode == 1
    calls = [json.loads(line) for line in calls_file.read_text().splitlines()]
    assert len([call for call in calls if "exec" in call]) == 1
    assert not (Path(environment["LOG_DIR"]) / "example_iter_1.md").exists()
    assert "No trusted Legacy frontier delta after iter=0" in completed.stderr
    assert "produced no trusted frontier progress" in completed.stderr


def test_legacy_waits_for_detached_collaboration_log_writer_before_frontier_check(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    calls_file = tmp_path / "codex-calls.jsonl"
    environment = _mock_environment(
        runner,
        fake_bin,
        mode="forged",
        extra_environment={
            "MAX_ITERATIONS": "1",
            "MOCK_CODEX_CALLS_FILE": str(calls_file),
            "MOCK_CODEX_DEFERRED_FRONTIER_SECONDS": "0.25",
        },
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )

    assert completed.returncode == 1
    calls = [json.loads(line) for line in calls_file.read_text().splitlines()]
    assert len([call for call in calls if "exec" in call]) == 1
    log = (Path(environment["LOG_DIR"]) / "example_iter_0.md").read_text(
        encoding="utf-8"
    )
    assert "foreground Codex turn returned" in log
    assert "deferred collaboration continuation complete" in log
    draft = runner.parent.parent / "results" / "example" / "blueprint.md"
    assert draft.read_text(encoding="utf-8") == (
        "deferred collaboration frontier\n"
    )
    assert "No trusted Legacy frontier delta" not in completed.stderr
    assert "Finished problem_id=example iter=0" in completed.stdout
    assert "Reached MAX_ITERATIONS=1" in completed.stderr


def test_legacy_accepts_publication_from_detached_collaboration_after_log_drain(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    calls_file = tmp_path / "codex-calls.jsonl"
    environment = _mock_environment(
        runner,
        fake_bin,
        mode="trusted",
        extra_environment={
            "MAX_ITERATIONS": "1",
            "MOCK_CODEX_CALLS_FILE": str(calls_file),
            "MOCK_CODEX_DEFERRED_FRONTIER_SECONDS": "0.25",
            "MOCK_CODEX_DEFERRED_ACTION": "publication",
        },
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    calls = [json.loads(line) for line in calls_file.read_text().splitlines()]
    assert len([call for call in calls if "exec" in call]) == 1
    log = (Path(environment["LOG_DIR"]) / "example_iter_0.md").read_text(
        encoding="utf-8"
    )
    assert "foreground Codex turn returned" in log
    assert "deferred collaboration publication complete" in log
    assert "Solved problem_id=example at iter=0" in completed.stdout
    assert "No trusted Legacy frontier delta" not in completed.stderr


def test_legacy_progress_allows_one_successor_but_not_an_unchanged_third(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    calls_file = tmp_path / "codex-calls.jsonl"
    environment = _mock_environment(
        runner,
        fake_bin,
        mode="forged",
        extra_environment={
            "MAX_ITERATIONS": "3",
            "MOCK_CODEX_CALLS_FILE": str(calls_file),
            "MOCK_FRONTIER_PROGRESS": "1",
            "MOCK_FRONTIER_PROGRESS_LIMIT": "1",
        },
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 1
    calls = [json.loads(line) for line in calls_file.read_text().splitlines()]
    assert len([call for call in calls if "exec" in call]) == 2
    assert not (Path(environment["LOG_DIR"]) / "example_iter_2.md").exists()
    assert "No trusted Legacy frontier delta after iter=1" in completed.stderr


@pytest.mark.parametrize("invalid_minutes", ("0", "9", "121", "sixty"))
def test_runner_rejects_invalid_deep_work_window_before_codex(
    tmp_path: Path,
    invalid_minutes: str,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    calls_file = tmp_path / "codex-calls.jsonl"
    environment = _mock_environment(
        runner,
        fake_bin,
        mode="forged",
        extra_environment={
            "RETHLAS_DEEP_WORK_MINUTES": invalid_minutes,
            "MOCK_CODEX_CALLS_FILE": str(calls_file),
        },
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )

    assert completed.returncode == 1
    assert "RETHLAS_DEEP_WORK_MINUTES" in completed.stderr
    assert not calls_file.exists()


def test_legacy_runner_drops_inherited_advisor_and_hotjoin_bindings(
    tmp_path: Path,
) -> None:
    completed = _run_mock(
        tmp_path,
        mode="trusted",
        extra_environment={
            "MOCK_EXPECT_NO_ADVISOR_ENV": "1",
            "MOCK_GUARDIAN_ENFORCEMENT_READY_MODE": "false",
            "RETHLAS_ADVISOR_RECEIPTS_ROOT": "/tmp/inherited-advisor-root",
            "RETHLAS_EXPECTED_HOTJOIN_RUN_ID": "stale-owner-run",
            "RETHLAS_GUARDIAN_ENFORCEMENT_READY": "false",
        },
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_runner_derives_exact_three_server_checkpoint_split_from_one_base(
    tmp_path: Path,
) -> None:
    completed = _run_mock(tmp_path, mode="trusted")
    assert completed.returncode == 0, completed.stdout + completed.stderr
    generation_root = tmp_path / "agents" / "generation"
    servers = json.loads(
        (generation_root / "reasoning_mcp_server_map_seen.json").read_text(
            encoding="utf-8"
        )
    )
    assert list(servers) == [
        "reasoning_agent",
        "reasoning_checkpoint_primary",
        "reasoning_checkpoint_recovery",
    ]
    reasoning = servers["reasoning_agent"]
    assert set(reasoning) == {
        "command",
        "args",
        "cwd",
        "env",
        "required",
        "tool_timeout_sec",
        "default_tools_approval_mode",
        "disabled_tools",
    }
    assert reasoning["default_tools_approval_mode"] == "approve"
    assert reasoning["required"] is True
    assert reasoning["tool_timeout_sec"] == 3600
    assert reasoning["disabled_tools"] == ["memory_append_batch"]
    for server in servers.values():
        assert "RETHLAS_BOUND_EXTERNAL_PLAN_PATH" not in server["env"]
        assert "RETHLAS_BOUND_EXTERNAL_PLAN_SHA256" not in server["env"]
        assert (
            "RETHLAS_BOUND_EXTERNAL_PLAN_ROOT_SESSION_ID"
            not in server["env"]
        )
    for checkpoint_id in (
        "reasoning_checkpoint_primary",
        "reasoning_checkpoint_recovery",
    ):
        checkpoint = servers[checkpoint_id]
        assert checkpoint["default_tools_approval_mode"] == "approve"
        assert checkpoint["required"] is True
        assert checkpoint["tool_timeout_sec"] == 60
        assert checkpoint["enabled_tools"] == ["memory_append_batch"]
    common_keys = {
        "command",
        "args",
        "cwd",
        "env",
        "required",
        "default_tools_approval_mode",
    }
    assert len(
        {
            json.dumps(
                {key: server[key] for key in common_keys},
                sort_keys=True,
                separators=(",", ":"),
            )
            for server in servers.values()
        }
    ) == 1
    assert reasoning["args"][:3] == ["-I", "-B", "-c"]
    commitments = reasoning["args"][4:]
    assert commitments[0::3] == list(LEGACY_TRUSTED_MCP_LOGICAL_MODULES)
    assert len(commitments) == 3 * len(LEGACY_TRUSTED_MCP_LOGICAL_MODULES)
    for offset in range(0, len(commitments), 3):
        module_name, module_path, module_sha256 = commitments[offset : offset + 3]
        assert module_name.startswith("mcp.")
        assert Path(module_path).is_absolute()
        assert not Path(module_path).is_relative_to(generation_root.resolve())
        assert len(module_sha256) == 64


def test_runner_injects_minimal_shell_path_with_preflighted_python(
    tmp_path: Path,
) -> None:
    completed = _run_mock(tmp_path, mode="trusted")
    assert completed.returncode == 0, completed.stdout + completed.stderr
    generation_root = tmp_path / "agents" / "generation"
    policy = json.loads(
        (generation_root / "shell_environment_policy_seen.json").read_text(
            encoding="utf-8"
        )
    )
    runtime_bin = tmp_path / "agents" / ".generation-venv" / "bin"
    assert policy == {
        "inherit": "none",
        "set": {
            "PATH": f"{runtime_bin}:/usr/bin:/bin:/usr/sbin:/sbin",
            "BASH_ENV": "/dev/null",
        },
    }


def test_runner_rejects_symlink_python_before_control_or_codex(
    tmp_path: Path,
) -> None:
    runner, runtime_bin = _make_runner_tree(tmp_path)
    python = runtime_bin / "python"
    python.unlink()
    python.symlink_to("python3")
    calls_file = tmp_path / "codex-calls.jsonl"
    environment = _mock_environment(
        runner,
        runtime_bin,
        mode="forged",
        extra_environment={
            "MOCK_CODEX_CALLS_FILE": str(calls_file),
            "RETHLAS_HOTJOIN_RUN_ID": "symlink-runtime-must-not-start",
        },
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )

    assert completed.returncode == 1
    assert "non-symlink Python interpreter" in completed.stderr
    assert "venv --copies" in completed.stderr
    assert not calls_file.exists()
    assert not (runner.parents[2] / ".rethlas_hotjoin").exists()


def test_runner_rejects_mismatched_python_alias_before_control_or_codex(
    tmp_path: Path,
) -> None:
    runner, runtime_bin = _make_runner_tree(tmp_path)
    python_alias = runtime_bin / "python3"
    python_alias.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    python_alias.chmod(0o755)
    calls_file = tmp_path / "codex-calls.jsonl"
    environment = _mock_environment(
        runner,
        runtime_bin,
        mode="forged",
        extra_environment={
            "MOCK_CODEX_CALLS_FILE": str(calls_file),
            "RETHLAS_HOTJOIN_RUN_ID": "mismatched-runtime-must-not-start",
        },
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )

    assert completed.returncode == 1
    assert "does not contain the selected interpreter bytes" in completed.stderr
    assert not calls_file.exists()
    assert not (runner.parents[2] / ".rethlas_hotjoin").exists()


@pytest.mark.parametrize("missing_module", REQUIRED_MODULES)
def test_runner_missing_runtime_module_starts_zero_codex_processes(
    tmp_path: Path,
    missing_module: str,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    shutil.rmtree(_module_stub(fake_bin, missing_module))
    calls_file = tmp_path / "codex-calls.jsonl"
    environment = _mock_environment(
        runner,
        fake_bin,
        mode="forged",
        extra_environment={"MOCK_CODEX_CALLS_FILE": str(calls_file)},
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )

    assert completed.returncode == 1
    assert missing_module in completed.stderr
    assert "module not found" in completed.stderr
    assert not calls_file.exists()


def test_runner_broken_runtime_import_starts_zero_codex_processes(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    (_module_stub(fake_bin, "sympy") / "__init__.py").write_text(
        "raise RuntimeError('mock native import failure')\n",
        encoding="utf-8",
    )
    calls_file = tmp_path / "codex-calls.jsonl"
    environment = _mock_environment(
        runner,
        fake_bin,
        mode="forged",
        extra_environment={"MOCK_CODEX_CALLS_FILE": str(calls_file)},
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )

    assert completed.returncode == 1
    assert (
        "sympy: import raised RuntimeError: mock native import failure"
        in completed.stderr
    )
    assert not calls_file.exists()


def test_runner_workspace_pth_entry_starts_zero_codex_processes(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    generation_root = runner.parent.parent
    workspace_package = generation_root / "sympy"
    workspace_package.mkdir()
    (workspace_package / "__init__.py").write_text("", encoding="utf-8")
    shutil.rmtree(_module_stub(fake_bin, "sympy"))
    (_site_packages(fake_bin) / "workspace-origin.pth").write_text(
        f"{generation_root}\n",
        encoding="utf-8",
    )
    calls_file = tmp_path / "codex-calls.jsonl"
    environment = _mock_environment(
        runner,
        fake_bin,
        mode="forged",
        extra_environment={"MOCK_CODEX_CALLS_FILE": str(calls_file)},
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=generation_root,
        env=environment,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )

    assert completed.returncode == 1
    assert ".pth path entry" in completed.stderr
    assert "model-writable generation workspace" in completed.stderr
    assert not calls_file.exists()


def test_runner_executable_pth_starts_zero_codex_processes(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    generation_root = runner.parent.parent
    workspace_package = generation_root / "sympy"
    workspace_package.mkdir()
    (workspace_package / "__init__.py").write_text("", encoding="utf-8")
    shutil.rmtree(_module_stub(fake_bin, "sympy"))
    site_packages = _site_packages(fake_bin)
    (site_packages / "workspace_editable_finder.py").write_text(
        """import importlib.abc
import importlib.util
import sys
from pathlib import Path

TARGET = Path({target!r})


class WorkspaceEditableFinder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname != "sympy":
            return None
        return importlib.util.spec_from_file_location(
            fullname,
            TARGET / "__init__.py",
            submodule_search_locations=[str(TARGET)],
        )


def install():
    sys.meta_path.insert(0, WorkspaceEditableFinder())
""".format(target=str(workspace_package)),
        encoding="utf-8",
    )
    (site_packages / "workspace-editable.pth").write_text(
        "import workspace_editable_finder; workspace_editable_finder.install()\n",
        encoding="utf-8",
    )
    calls_file = tmp_path / "codex-calls.jsonl"
    environment = _mock_environment(
        runner,
        fake_bin,
        mode="forged",
        extra_environment={"MOCK_CODEX_CALLS_FILE": str(calls_file)},
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=generation_root,
        env=environment,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )

    assert completed.returncode == 1
    assert "executable .pth line is forbidden" in completed.stderr
    assert not calls_file.exists()


def test_runner_workspace_editable_origin_starts_zero_codex_processes(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    generation_root = runner.parent.parent
    workspace_package = generation_root / "sympy"
    workspace_package.mkdir()
    (workspace_package / "__init__.py").write_text("", encoding="utf-8")
    shutil.rmtree(_module_stub(fake_bin, "sympy"))
    editable_packages = fake_bin.parent / "editable-packages"
    editable_packages.mkdir()
    (editable_packages / "sympy").symlink_to(
        workspace_package, target_is_directory=True
    )
    (_site_packages(fake_bin) / "workspace-editable-origin.pth").write_text(
        f"{editable_packages}\n",
        encoding="utf-8",
    )
    calls_file = tmp_path / "codex-calls.jsonl"
    environment = _mock_environment(
        runner,
        fake_bin,
        mode="forged",
        extra_environment={"MOCK_CODEX_CALLS_FILE": str(calls_file)},
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=generation_root,
        env=environment,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )

    assert completed.returncode == 1
    assert "sympy: unsafe module spec" in completed.stderr
    assert "model-writable generation workspace" in completed.stderr
    assert not calls_file.exists()


@pytest.mark.parametrize("package_kind", ("regular", "namespace"))
def test_runner_accepts_safe_external_required_package_locations(
    tmp_path: Path,
    package_kind: str,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    if package_kind == "namespace":
        (_module_stub(fake_bin, "sympy") / "__init__.py").unlink()
    environment = _mock_environment(runner, fake_bin, mode="trusted")

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_runner_rejects_model_written_verified_file_without_receipt(
    tmp_path: Path,
) -> None:
    completed = _run_mock(tmp_path, mode="forged")
    assert completed.returncode == 1
    assert "without verified publication" in completed.stderr


def test_runner_stops_if_model_tampers_with_publisher_runtime(tmp_path: Path) -> None:
    completed = _run_mock(tmp_path, mode="tamper")
    assert completed.returncode == 70
    assert "runtime was modified" in completed.stderr


def test_runner_pins_mcp_restart_to_external_attested_snapshot(
    tmp_path: Path,
) -> None:
    completed = _run_mock(tmp_path, mode="transient_tamper")
    assert completed.returncode == 1, completed.stdout + completed.stderr
    generation_root = tmp_path / "agents" / "generation"
    snapshot_marker = generation_root / "snapshot_restart_checked"
    assert snapshot_marker.exists()
    snapshot_server = Path(snapshot_marker.read_text(encoding="utf-8"))
    assert snapshot_server.exists()
    assert not snapshot_server.is_relative_to(generation_root.resolve())
    assert "runtime was modified" not in completed.stderr


def test_secure_loader_rejects_mutate_restore_during_mcp_restart(
    tmp_path: Path,
) -> None:
    completed = _run_mock(tmp_path, mode="snapshot_restart_tamper")
    assert completed.returncode == 1, completed.stdout + completed.stderr
    generation_root = tmp_path / "agents" / "generation"
    checked = generation_root / "snapshot_restart_loader_checked"
    executed = generation_root / "snapshot_restart_payload_executed"
    assert checked.exists()
    snapshot_server = Path(checked.read_text(encoding="utf-8"))
    assert snapshot_server.exists()
    assert not snapshot_server.is_relative_to(generation_root.resolve())
    assert not executed.exists()


def test_runner_rejects_unchecked_hash_bytecode_before_codex_starts(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    generation_root = runner.parent.parent
    malicious_source = tmp_path / "malicious_verification_client.py"
    malicious_source.write_text(
        "MARKER = 'malicious bytecode loaded'\n", encoding="utf-8"
    )
    cache_dir = generation_root / "mcp" / "__pycache__"
    cache_dir.mkdir()
    bytecode_path = (
        cache_dir / f"verification_client.{sys.implementation.cache_tag}.pyc"
    )
    py_compile.compile(
        str(malicious_source),
        cfile=str(bytecode_path),
        doraise=True,
        invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH,
    )
    environment = _mock_environment(runner, fake_bin, mode="forged")

    completed = subprocess.run(
        [str(runner)],
        cwd=generation_root,
        env=environment,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )

    assert completed.returncode == 1
    assert "Python bytecode cache directory is forbidden" in completed.stderr
    assert not (generation_root / "results").exists()


def test_runner_rejects_python_environment_inside_generation_workspace(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    generation_root = runner.parent.parent
    writable_venv = generation_root / ".venv"
    subprocess.run(
        [sys.executable, "-m", "venv", "--copies", str(writable_venv)],
        check=True,
        capture_output=True,
        text=True,
    )
    environment = _mock_environment(runner, fake_bin, mode="forged")
    environment["RETHLAS_GENERATION_PYTHON_BIN"] = str(
        writable_venv / "bin" / "python"
    )
    environment["PATH"] = (
        f"{writable_venv / 'bin'}{os.pathsep}"
        f"{fake_bin}{os.pathsep}{environment['PATH']}"
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=generation_root,
        env=environment,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )

    assert completed.returncode == 1
    assert "Python environment must be outside" in completed.stderr
    assert not (generation_root / "results").exists()


def test_runner_rejects_problem_name_that_would_be_normalized(tmp_path: Path) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    (runner.parent.parent / "data" / "foo bar.md").write_text("S", encoding="utf-8")
    environment = _mock_environment(
        runner,
        fake_bin,
        mode="forged",
        problem_file="data/foo bar.md",
    )
    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )
    assert completed.returncode == 1
    assert "Unsupported problem path component" in completed.stderr


def test_runner_rejects_symlinked_problem_before_any_codex_call(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    generation_root = runner.parent.parent
    outside = tmp_path / "external-problem.md"
    outside.write_text("external statement", encoding="utf-8")
    problem = generation_root / "data" / "example.md"
    problem.unlink()
    problem.symlink_to(outside)
    calls_file = tmp_path / "codex-calls.jsonl"
    environment = _mock_environment(
        runner,
        fake_bin,
        mode="forged",
        extra_environment={"MOCK_CODEX_CALLS_FILE": str(calls_file)},
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=generation_root,
        env=environment,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )

    assert completed.returncode == 1
    assert "non-symlink Markdown file" in completed.stderr
    assert not calls_file.exists()


def test_runner_forwards_explicit_https_endpoint_and_api_token(tmp_path: Path) -> None:
    completed = _run_mock(
        tmp_path,
        mode="trusted",
        extra_environment={
            "VERIFY_PROOF_URL": "https://verifier.example/verify",
            "VERIFY_API_TOKEN": "mock-secret-token",
            "MOCK_EXPECT_VERIFY_PROOF_URL": "https://verifier.example/verify",
            "MOCK_EXPECT_VERIFY_API_TOKEN": "mock-secret-token",
        },
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
