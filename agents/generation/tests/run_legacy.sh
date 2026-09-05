#!/usr/bin/env bash
set -euo pipefail

if (( BASH_VERSINFO[0] < 5 )); then
  echo "AxiomRelay requires Bash 5 or newer (macOS: brew install bash)." >&2
  exit 1
fi

descriptor_root=""
for candidate in /proc/self/fd /dev/fd; do
  if [[ -d "$candidate" ]]; then
    descriptor_root="$candidate"
    break
  fi
done
if [[ -z "$descriptor_root" ]]; then
  echo "Could not locate a descriptor filesystem (/proc/self/fd or /dev/fd)." >&2
  exit 70
fi
descriptor_execution_supported=1
if [[ "$OSTYPE" == darwin* ]]; then
  # Darwin permits reads through /dev/fd/N but rejects executable images at
  # that pathname. Keep each inherited fd for digest/identity checks and use
  # its separately bound origin path for process creation.
  descriptor_execution_supported=0
fi

OWNED_RUNNER_ORIGIN="${RETHLAS_OWNED_EXECUTABLE_ORIGIN:-}"
OWNED_RUNNER_FD="${RETHLAS_OWNED_EXECUTABLE_FD:-}"
OWNED_RUNNER_SHA256="${RETHLAS_OWNED_EXECUTABLE_SHA256:-}"
if [[ -n "$OWNED_RUNNER_ORIGIN" || -n "$OWNED_RUNNER_FD" \
   || -n "$OWNED_RUNNER_SHA256" ]]; then
  if [[ "$OWNED_RUNNER_ORIGIN" != /* \
     || ! "$OWNED_RUNNER_FD" =~ ^[0-9]+$ \
     || ! "$OWNED_RUNNER_SHA256" =~ ^[0-9a-f]{64}$ \
     || ! -r "$descriptor_root/$OWNED_RUNNER_FD" ]]; then
    echo "Owned legacy runner binding is invalid." >&2
    exit 70
  fi
  ROOT_DIR="$(cd "$(dirname "$OWNED_RUNNER_ORIGIN")/.." && pwd -P)"
  if [[ "$OWNED_RUNNER_ORIGIN" != "$ROOT_DIR/tests/run_legacy.sh" ]]; then
    echo "Owned legacy runner origin differs from the generation runtime." >&2
    exit 70
  fi
  LEGACY_RUNNER_SOURCE="$descriptor_root/$OWNED_RUNNER_FD"
else
  ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
  LEGACY_RUNNER_SOURCE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/$(basename "${BASH_SOURCE[0]}")"
fi
unset RETHLAS_OWNED_EXECUTABLE_ORIGIN RETHLAS_OWNED_EXECUTABLE_FD
unset RETHLAS_OWNED_EXECUTABLE_SHA256
PROBLEM_FILE="${PROBLEM_FILE:-}"
if [[ -z "$PROBLEM_FILE" ]]; then
  echo "PROBLEM_FILE is required (for example data/my_problem.md)." >&2
  exit 1
fi
MODEL_POLICY_PROFILE="${RETHLAS_MODEL_POLICY_PROFILE:-compatible}"
generator_default_model="gpt-6-astra"
if [[ "$MODEL_POLICY_PROFILE" == economy ]]; then
  generator_default_model="gpt-5.6-terra"
fi
MODEL="${MODEL:-$generator_default_model}"
if [[ "$MODEL" == gpt-5.6-sol ]]; then
  echo "gpt-5.6-sol is historical only; use gpt-6-astra for new runs." >&2
  exit 1
fi
REASONING_EFFORT="${REASONING_EFFORT:-max}"
MAIN_AGENT_SELECTION="${RETHLAS_MAIN_AGENT:-gpt-astra}"
if [[ "$MAIN_AGENT_SELECTION" == gpt-sol ]]; then
  MAIN_AGENT_SELECTION="gpt-astra"
fi
EXTERNAL_PLAN_SET_SELECTION="${RETHLAS_EXTERNAL_PLAN_SET:-}"
EXTERNAL_PLAN_SHA256="${RETHLAS_EXTERNAL_PLAN_SHA256:-}"
MAX_ITERATIONS="${MAX_ITERATIONS:-10}"
DEEP_WORK_MINUTES="${RETHLAS_DEEP_WORK_MINUTES:-60}"
TIMER_INTERVAL_SECONDS="${TIMER_INTERVAL_SECONDS:-30}"
ALLOW_OFFLINE_DRAFT="${RETHLAS_LEGACY_ALLOW_OFFLINE_DRAFT:-0}"
STOP_AFTER_CURRENT_COHORT="${RETHLAS_LEGACY_STOP_AFTER_CURRENT_COHORT:-0}"
GENERATION_PYTHON_SELECTION="${RETHLAS_GENERATION_PYTHON_BIN:-}"
COHORT_CODEX_FD="${RETHLAS_COHORT_CODEX_FD:-}"
COHORT_HOST_SOURCE_FD="${RETHLAS_COHORT_HOST_SOURCE_FD:-}"
COHORT_HOST_SOURCE_SNAPSHOT="${RETHLAS_COHORT_HOST_SOURCE_SNAPSHOT:-}"
COHORT_HOST_SOURCE_ORIGIN="${RETHLAS_COHORT_HOST_SOURCE_ORIGIN:-}"
COHORT_HOST_SOURCE_SHA256="${RETHLAS_COHORT_HOST_SOURCE_SHA256:-}"
unset RETHLAS_GENERATION_PYTHON_BIN
NONFRESH_CONTROL_ONLY=0
REVIEW_CADENCE_POLICY="disabled"

case "${RETHLAS_RUN_MODE:-core}" in
  core|legacy) ;;
  *)
    echo "run_legacy.sh accepts only RETHLAS_RUN_MODE=core (legacy alias)." >&2
    exit 1
    ;;
esac
export RETHLAS_RUN_MODE="core"
case "$MODEL_POLICY_PROFILE" in
  compatible|balanced|economy|max_diversity) ;;
  *) echo "Unsupported RETHLAS_MODEL_POLICY_PROFILE: $MODEL_POLICY_PROFILE" >&2; exit 1 ;;
esac
export RETHLAS_MODEL_POLICY_PROFILE="$MODEL_POLICY_PROFILE"

if [[ -n "${RETHLAS_HOTJOIN_RUN_ID:-}" ]]; then
  echo "The isolated legacy runner does not accept RETHLAS_HOTJOIN_RUN_ID." >&2
  exit 1
fi
if [[ "${RETHLAS_REVIEW_CADENCE_POLICY:-disabled}" != disabled \
   || "${RETHLAS_CONTEXT_GUARD_POLICY:-disabled}" != disabled ]]; then
  echo "Durable review cadence/context guard require RETHLAS_HOTJOIN_RUN_ID; the isolated legacy runner cannot enable them." >&2
  exit 1
fi
if [[ "${RETHLAS_NONFRESH_RESUME_DRY_RUN:-0}" != 0 \
   || "${RETHLAS_NONFRESH_STALE_RECONCILE:-0}" != 0 \
   || -n "${RETHLAS_NONFRESH_RESUME_DB_COPY:-}" ]]; then
  echo "Copied-ledger recovery belongs to the hot-join control runner." >&2
  exit 1
fi
unset RETHLAS_HOTJOIN_RUN_ID RETHLAS_REVIEW_CADENCE_POLICY
unset RETHLAS_CONTEXT_GUARD_POLICY RETHLAS_NONFRESH_RESUME_DRY_RUN
unset RETHLAS_NONFRESH_STALE_RECONCILE RETHLAS_NONFRESH_RESUME_DB_COPY
unset RETHLAS_ADVISOR_RECEIPTS_ROOT RETHLAS_EXPECTED_HOTJOIN_RUN_ID
unset RETHLAS_POLICY_CONTRACT_SHA256 RETHLAS_REVIEW_ADAPTER_PATH
unset RETHLAS_REVIEW_ADAPTER_SHA256 RETHLAS_REVIEW_DB
unset RETHLAS_REVIEW_CONTROL_TOKEN RETHLAS_GUARDIAN_CYCLE_TOKEN
unset RETHLAS_RUNNER_CYCLE_TOKEN RETHLAS_STALE_RECOVERY_TOKEN
unset RETHLAS_REVIEW_EXPECTED_MODEL RETHLAS_REVIEW_EXPECTED_REASONING_EFFORT
unset RETHLAS_REVIEW_POLICY_SHA256 RETHLAS_REVIEW_CONTRACT_CLI_PATH
unset RETHLAS_REVIEW_CONTRACT_CLI_SHA256
unset RETHLAS_LEGACY_ALLOW_OFFLINE_DRAFT
unset RETHLAS_LEGACY_STOP_AFTER_CURRENT_COHORT
unset RETHLAS_MAIN_AGENT RETHLAS_CLAUDE_BIN
unset RETHLAS_EXTERNAL_PLAN_SET RETHLAS_EXTERNAL_PLAN_SHA256
unset RETHLAS_BOUND_EXTERNAL_PLAN_PATH
unset RETHLAS_BOUND_EXTERNAL_PLAN_SHA256
unset RETHLAS_BOUND_EXTERNAL_PLAN_ROOT_SESSION_ID

if [[ "$MAIN_AGENT_SELECTION" != gpt-astra ]]; then
  echo "run_legacy.sh is the GPT Astra root/cohort executor; use run_example.sh for Claude root." >&2
  exit 1
fi
if [[ -n "$EXTERNAL_PLAN_SET_SELECTION" || -n "$EXTERNAL_PLAN_SHA256" ]]; then
  if [[ -z "$EXTERNAL_PLAN_SET_SELECTION" || -z "$EXTERNAL_PLAN_SHA256" ]]; then
    echo "External Claude cohort execution requires both plan path and SHA-256." >&2
    exit 1
  fi
  if [[ "$MAIN_AGENT_SELECTION" != gpt-astra ]]; then
    echo "External Claude plans require the GPT Astra cohort executor." >&2
    exit 1
  fi
  if [[ "$STOP_AFTER_CURRENT_COHORT" != 1 || "$MAX_ITERATIONS" != 1 ]]; then
    echo "External Claude plans require one root and stop-after-current-cohort." >&2
    exit 1
  fi
fi
EXTERNAL_PLAN_USED=0
EXTERNAL_PLAN_PATH=""
EXTERNAL_PLAN_RELATIVE=""
EXTERNAL_PLAN_ROOT_SESSION_ID=""
EXTERNAL_RETRIEVAL_MODE="disabled"
COHORT_NAMESPACE_PROGRAM=""
COHORT_ISOLATION_BACKEND=""
COHORT_PERMISSION_CONFIG_ARGS=()

# The generation runtime is content-attested below. Never create interpreter
# caches in that trusted tree: bytecode is executable input, not a disposable
# artifact, and therefore cannot be excluded safely from the trust decision.
export PYTHONDONTWRITEBYTECODE=1

REQUIRED_GENERATION_MODULES=(
  mcp
  requests
  numpy
  scipy
  sympy
  mpmath
  gmpy2
)

# Resolve and constrain the interpreter with shell built-ins/tools before using
# Python for any trust decision. In particular, never execute a model-writable
# virtual environment and then ask that same interpreter whether it is safe.
if [[ -n "$GENERATION_PYTHON_SELECTION" ]]; then
  python_command="$GENERATION_PYTHON_SELECTION"
elif [[ -x "$ROOT_DIR/../.generation-venv/bin/python" ]]; then
  python_command="$ROOT_DIR/../.generation-venv/bin/python"
else
  python_command="$(command -v python3 || true)"
fi
if [[ "$python_command" != /* ]] || [[ ! -x "$python_command" ]]; then
  echo "Generation python3 must resolve to an absolute executable path." >&2
  exit 1
fi
TRUSTED_PYTHON_BIN="$(cd "$(dirname "$python_command")" && pwd -P)/$(basename "$python_command")"
unset RETHLAS_COST_GATE_POLICY RETHLAS_RESOLVED_COST_POLICY_JSON
unset RETHLAS_RESOLVED_COST_POLICY_SHA256
python_target="$TRUSTED_PYTHON_BIN"
temporary_root="$(cd "${TMPDIR:-/tmp}" && pwd -P)"
for candidate in "$TRUSTED_PYTHON_BIN" "$python_target"; do
  if [[ "$candidate" == "$ROOT_DIR" || "$candidate" == "$ROOT_DIR"/* \
     || "$candidate" == "$temporary_root" || "$candidate" == "$temporary_root"/* ]]; then
    echo "Python environment must be outside the generation workspace and temporary directory: $candidate" >&2
    exit 1
  fi
done
if [[ -L "$TRUSTED_PYTHON_BIN" \
   || "$TRUSTED_PYTHON_BIN" != "$python_target" ]]; then
  echo "Guardian requires a non-symlink Python interpreter; recreate the external generation environment with: python3 -m venv --copies <path>" >&2
  exit 1
fi

# Process .pth files before starting Python with site initialization enabled.
# Executable .pth lines run before the in-process preflight can inspect
# sys.path/spec origins, so a PEP 660/editable hook could otherwise execute
# model-writable code first. Use -S here to keep this scan ahead of all site
# hooks, and require an isolated environment rather than system-site fallback.
if ! "$TRUSTED_PYTHON_BIN" -I -S -B - \
  "$ROOT_DIR" "$temporary_root" "$TRUSTED_PYTHON_BIN" <<'PY'
import os
import stat
import sys
from pathlib import Path

root = Path(sys.argv[1]).resolve(strict=True)
temporary_root = Path(sys.argv[2]).resolve(strict=True)
expected_executable = Path(sys.argv[3]).absolute()
executable = Path(sys.executable).absolute()


def fail(message: str) -> None:
    print(f"generation math-research runtime .pth preflight failed: {message}", file=sys.stderr)
    raise SystemExit(2)


def resolved_outside_writable(value: object, label: str) -> Path:
    try:
        raw = os.fspath(value)
    except TypeError as exc:
        fail(f"{label} is not a filesystem path: {value!r}: {exc}")
    candidate = Path(os.fsdecode(raw))
    try:
        resolved = candidate.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        fail(f"cannot resolve {label} {candidate}: {exc}")
    for boundary_label, boundary in (
        ("generation workspace", root),
        ("temporary directory", temporary_root),
    ):
        if resolved == boundary or resolved.is_relative_to(boundary):
            fail(f"{label} resolves inside the model-writable {boundary_label}: {resolved}")
    return resolved


if executable != expected_executable:
    fail(f"Python executable changed during .pth validation: {executable}")
scripts_dir = resolved_outside_writable(expected_executable.parent, "Python bin directory")
environment_root = resolved_outside_writable(scripts_dir.parent, "Python environment")
venv_config = environment_root / "pyvenv.cfg"
if venv_config.exists() or venv_config.is_symlink():
    metadata = venv_config.lstat()
    if not stat.S_ISREG(metadata.st_mode):
        fail(f"pyvenv.cfg must be a regular non-symlink file: {venv_config}")
    if metadata.st_size > 65536:
        fail(f"pyvenv.cfg is unexpectedly large: {venv_config}")
    try:
        config_text = venv_config.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        fail(f"cannot read pyvenv.cfg: {exc}")
    for line in config_text.splitlines():
        key, separator, value = line.partition("=")
        if separator and key.strip().casefold() == "include-system-site-packages":
            if value.strip().casefold() == "true":
                fail("include-system-site-packages must be false")

site_directories: set[Path] = set()
for library_name in ("lib", "lib64"):
    library_root = environment_root / library_name
    if not library_root.is_dir():
        continue
    for version_directory in library_root.glob("python*"):
        candidate = version_directory / "site-packages"
        if candidate.is_dir() or candidate.is_symlink():
            site_directories.add(
                resolved_outside_writable(candidate, "Python site-packages directory")
            )
windows_site = environment_root / "Lib" / "site-packages"
if windows_site.is_dir() or windows_site.is_symlink():
    site_directories.add(
        resolved_outside_writable(windows_site, "Python site-packages directory")
    )

for site_directory in sorted(site_directories):
    try:
        pth_files = sorted(site_directory.glob("*.pth"))
    except OSError as exc:
        fail(f"cannot enumerate .pth files in {site_directory}: {exc}")
    for pth_file in pth_files:
        try:
            metadata = pth_file.lstat()
        except OSError as exc:
            fail(f"cannot inspect .pth file {pth_file}: {exc}")
        if not stat.S_ISREG(metadata.st_mode):
            fail(f".pth entry must be a regular non-symlink file: {pth_file}")
        if metadata.st_size > 1_000_000:
            fail(f".pth file exceeds 1 MB: {pth_file}")
        try:
            text = pth_file.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            fail(f"cannot read .pth file {pth_file}: {exc}")
        for line_number, line in enumerate(text.splitlines(), start=1):
            processed = line.rstrip()
            stripped = processed.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if stripped.startswith(("import ", "import\t")):
                fail(f"executable .pth line is forbidden: {pth_file}:{line_number}")
            resolved_outside_writable(
                site_directory / processed,
                f".pth path entry {pth_file}:{line_number}",
            )
PY
then
  echo "Use a wheel-installed, isolated generation environment without executable .pth hooks." >&2
  exit 1
fi

# This is the interpreter used both by the immutable MCP snapshot and by the
# model's local math shell. Validate it, then import every declared runtime
# module before creating run state, taking a snapshot, or invoking Codex.
if ! "$TRUSTED_PYTHON_BIN" -I -B - \
  "$ROOT_DIR" "$TRUSTED_PYTHON_BIN" "$NONFRESH_CONTROL_ONLY" \
  "${REQUIRED_GENERATION_MODULES[@]}" <<'PY'
import importlib
import importlib.util
import hashlib
import os
import stat
import sys
import tempfile
from pathlib import Path

root = Path(sys.argv[1]).resolve(strict=True)
expected_executable = Path(sys.argv[2]).absolute()
if sys.argv[3] not in {"0", "1"}:
    raise SystemExit("invalid copied-ledger control mode")
nonfresh_control_only = sys.argv[3] == "1"
module_names = [] if nonfresh_control_only else sys.argv[4:]
executable = Path(sys.executable).absolute()
prefix = Path(sys.prefix).resolve(strict=True)
temporary_root = Path(tempfile.gettempdir()).resolve(strict=True)


def fail(message: str) -> None:
    print(f"generation math-research runtime preflight failed: {message}", file=sys.stderr)
    raise SystemExit(2)


class UnsafeRuntimePath(RuntimeError):
    pass


def audit_filesystem_path(value: object, label: str) -> None:
    try:
        raw = os.fspath(value)
    except TypeError as exc:
        raise UnsafeRuntimePath(f"{label} is not a filesystem path: {value!r}") from exc
    if not isinstance(raw, str):
        raw = os.fsdecode(raw)
    candidate = Path.cwd() if raw == "" else Path(raw)
    try:
        resolved = candidate.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise UnsafeRuntimePath(f"cannot resolve {label} {candidate}: {exc}") from exc
    for boundary_label, boundary in (
        ("generation workspace", root),
        ("temporary directory", temporary_root),
    ):
        if resolved == boundary or resolved.is_relative_to(boundary):
            raise UnsafeRuntimePath(
                f"{label} resolves inside the model-writable {boundary_label}: {resolved}"
            )


def audit_path_collection(values: object, label: str) -> list[object]:
    try:
        entries = list(values)  # type: ignore[arg-type]
    except BaseException as exc:
        raise UnsafeRuntimePath(f"cannot inspect {label}: {type(exc).__name__}: {exc}") from exc
    for index, entry in enumerate(entries):
        audit_filesystem_path(entry, f"{label}[{index}]")
    return entries


def audit_spec(spec: object, label: str) -> None:
    origin = getattr(spec, "origin", None)
    locations = getattr(spec, "submodule_search_locations", None)
    if origin not in (None, "built-in", "frozen"):
        audit_filesystem_path(origin, f"{label} spec.origin")
    if locations is not None:
        entries = audit_path_collection(locations, f"{label} spec search path")
        if origin is None and not entries:
            raise UnsafeRuntimePath(f"{label} namespace package has no search locations")
    elif origin is None:
        raise UnsafeRuntimePath(f"{label} has neither an origin nor package search locations")


def audit_sys_path(stage: str) -> None:
    for index, entry in enumerate(sys.path):
        audit_filesystem_path(entry, f"sys.path[{index}] during {stage}")


def audit_loaded_module_tree(module_name: str) -> None:
    prefix = module_name + "."
    for loaded_name, loaded_module in list(sys.modules.items()):
        if loaded_module is None or not (
            loaded_name == module_name or loaded_name.startswith(prefix)
        ):
            continue
        spec = getattr(loaded_module, "__spec__", None)
        if spec is not None:
            audit_spec(spec, f"loaded module {loaded_name}")
        for attribute in ("__file__", "__cached__"):
            value = getattr(loaded_module, attribute, None)
            if value is not None:
                audit_filesystem_path(value, f"loaded module {loaded_name}.{attribute}")
        package_path = getattr(loaded_module, "__path__", None)
        if package_path is not None:
            audit_path_collection(package_path, f"loaded module {loaded_name}.__path__")


if executable != expected_executable:
    fail(f"Python executable changed during validation: {executable}")
if prefix.is_relative_to(root) or prefix.is_relative_to(temporary_root):
    fail(
        "Python environment must be outside the generation workspace and "
        f"temporary directory: {prefix}"
    )
try:
    audit_sys_path("initial runtime validation")
except UnsafeRuntimePath as exc:
    fail(str(exc))

scripts_dir = expected_executable.parent.resolve(strict=True)
if os.pathsep in str(scripts_dir) or "\n" in str(scripts_dir):
    fail(f"Python bin directory cannot be represented safely in PATH: {scripts_dir}")
command_names = ("python3",) if nonfresh_control_only else ("python", "python3")
expected_digest = hashlib.sha256(expected_executable.read_bytes()).digest()
for command_name in command_names:
    candidate = scripts_dir / command_name
    try:
        candidate_metadata = candidate.lstat()
    except OSError as exc:
        fail(f"{command_name} is missing from the trusted Python bin directory: {exc}")
    if (
        candidate.is_symlink()
        or not stat.S_ISREG(candidate_metadata.st_mode)
        or candidate_metadata.st_uid not in {0, os.geteuid()}
        or candidate_metadata.st_nlink != 1
        or stat.S_IMODE(candidate_metadata.st_mode) & 0o022
        or stat.S_IMODE(candidate_metadata.st_mode) & 0o111 == 0
        or not os.access(candidate, os.X_OK)
    ):
        fail(f"{candidate} is not a pinned-executable-compatible regular file")
    if hashlib.sha256(candidate.read_bytes()).digest() != expected_digest:
        fail(f"{candidate} does not contain the selected interpreter bytes")

errors: list[str] = []
for module_name in module_names:
    try:
        spec = importlib.util.find_spec(module_name)
    except BaseException as exc:  # fail closed for broken package metadata/hooks
        errors.append(f"{module_name}: find_spec raised {type(exc).__name__}: {exc}")
        continue
    if spec is None:
        errors.append(f"{module_name}: module not found")
        continue
    try:
        audit_spec(spec, f"required module {module_name}")
    except BaseException as exc:
        errors.append(f"{module_name}: unsafe module spec: {type(exc).__name__}: {exc}")
        continue
    try:
        importlib.import_module(module_name)
    except BaseException as exc:  # an installed package can still be unusable
        errors.append(f"{module_name}: import raised {type(exc).__name__}: {exc}")
        continue
    try:
        audit_sys_path(f"import of {module_name}")
        audit_loaded_module_tree(module_name)
    except BaseException as exc:
        errors.append(f"{module_name}: unsafe imported path: {type(exc).__name__}: {exc}")

if "mcp" in module_names and not any(
    error.startswith("mcp:") for error in errors
):
    try:
        try:
            sdk_server = importlib.import_module("mcp.server.fastmcp")
            server_class = getattr(sdk_server, "FastMCP")
        except (ImportError, AttributeError):
            sdk_server = importlib.import_module("mcp.server.mcpserver")
            server_class = getattr(sdk_server, "MCPServer")
        if not callable(server_class):
            raise TypeError("resolved MCP server class is not callable")
        audit_sys_path("official MCP server compatibility import")
        audit_loaded_module_tree("mcp")
    except BaseException as exc:
        errors.append(
            "mcp: compatible FastMCP/MCPServer import raised "
            f"{type(exc).__name__}: {exc}"
        )

if errors:
    fail("; ".join(errors))
PY
then
  if [[ "$NONFRESH_CONTROL_ONLY" == 1 ]]; then
    echo "Use an isolated trusted Python 3 interpreter for the copied-ledger operation." >&2
  else
    echo "Install agents/generation/requirements-math-research.txt into the selected external Python environment." >&2
  fi
  exit 1
fi
trusted_python_command="$TRUSTED_PYTHON_BIN"
trusted_python_dir="$(cd "$(dirname "$trusted_python_command")" && pwd -P)"
SAFE_SHELL_PATH="$trusted_python_dir:/usr/bin:/bin:/usr/sbin:/sbin"
# Codex builds its shell snapshot before applying shell_environment_policy.  In
# a user namespace the mapped UID 0 can make that snapshot probe /root/.bashrc;
# bind the Codex process itself to an inert Bash startup file as well as binding
# every later tool shell through the policy below.
export BASH_ENV="/dev/null"
TRUSTED_SHELL_ENVIRONMENT_POLICY_TOML="$(
  "$TRUSTED_PYTHON_BIN" -I -B -c \
    'import json, sys; print("{inherit=\"none\",set={PATH=" + json.dumps(sys.argv[1]) + ",BASH_ENV=\"/dev/null\"}}")' \
    "$SAFE_SHELL_PATH"
)"

# Resolve every component before any Codex invocation.  A final or ancestor
# symlink could otherwise make a syntactically data-relative path read an
# external statement and consume paid tokens before the publication layer
# rejects the mismatch.
if ! "$TRUSTED_PYTHON_BIN" -I -B - "$ROOT_DIR" "$PROBLEM_FILE" <<'PY'
import pathlib
import sys

try:
    root = pathlib.Path(sys.argv[1]).resolve(strict=True)
    relative = pathlib.Path(sys.argv[2])
    data_root = root / "data"
    cursor = root
    for component in relative.parts:
        cursor /= component
        if cursor.is_symlink():
            raise ValueError(f"symlink component is forbidden: {cursor}")
    resolved_data = data_root.resolve(strict=True)
    resolved_problem = (root / relative).resolve(strict=True)
    if not resolved_data.is_dir() or not resolved_data.is_relative_to(root):
        raise ValueError("data root escapes the generation workspace")
    if not resolved_problem.is_file() or not resolved_problem.is_relative_to(resolved_data):
        raise ValueError("problem file escapes the authenticated data root")
except (OSError, ValueError) as exc:
    print(f"Unsafe problem file: {exc}", file=sys.stderr)
    raise SystemExit(1)
PY
then
  exit 1
fi

if ! [[ "$MAX_ITERATIONS" =~ ^[0-9]+$ ]] || [[ "$MAX_ITERATIONS" -le 0 ]]; then
  echo "MAX_ITERATIONS must be a positive integer: $MAX_ITERATIONS" >&2
  exit 1
fi
if [[ "$ALLOW_OFFLINE_DRAFT" != 0 && "$ALLOW_OFFLINE_DRAFT" != 1 ]]; then
  echo "RETHLAS_LEGACY_ALLOW_OFFLINE_DRAFT must be 0 or 1." >&2
  exit 1
fi
if [[ "$STOP_AFTER_CURRENT_COHORT" != 0 \
   && "$STOP_AFTER_CURRENT_COHORT" != 1 ]]; then
  echo "RETHLAS_LEGACY_STOP_AFTER_CURRENT_COHORT must be 0 or 1." >&2
  exit 1
fi

if ! [[ "$DEEP_WORK_MINUTES" =~ ^[0-9]+$ ]] \
   || [[ "$DEEP_WORK_MINUTES" -lt 10 ]] \
   || [[ "$DEEP_WORK_MINUTES" -gt 120 ]]; then
  echo "RETHLAS_DEEP_WORK_MINUTES must be an integer from 10 through 120: $DEEP_WORK_MINUTES" >&2
  exit 1
fi
if [[ "$REVIEW_CADENCE_POLICY" != disabled \
   && "$DEEP_WORK_MINUTES" -ne 60 ]]; then
  echo "RETHLAS_DEEP_WORK_MINUTES must be 60 under a durable hot-join policy." >&2
  exit 1
fi

# data/algebra/prob1.md -> algebra/prob1
problem_rel="${PROBLEM_FILE#data/}"
problem_rel="${problem_rel%.md}"
problem_name="$(basename "$PROBLEM_FILE" .md)"
ref_dir="data/${problem_rel}.refs"
ref_prompt="Use reference_dir=${ref_dir} if it exists."
unset LEGACY_GENERATION_CONTROL_TOKEN
LEGACY_GENERATION_CONTROL_TOKEN="$("$TRUSTED_PYTHON_BIN" -I -B -c \
  'import secrets; print(secrets.token_hex(16))')"
readonly LEGACY_GENERATION_CONTROL_TOKEN
# Never inherit or export a generation-control capability. Only the two
# owner-side CLI calls below receive this token in their one-process environment.
unset RETHLAS_GENERATION_CONTROL_TOKEN
unset RETHLAS_REVIEW_CONTROL_TOKEN
unset RETHLAS_GUARDIAN_CYCLE_TOKEN
unset RETHLAS_RUNNER_CYCLE_TOKEN
unset RETHLAS_STALE_RECOVERY_TOKEN
RETHLAS_REVIEW_CONTROL_TOKEN=""
if [[ "$REVIEW_CADENCE_POLICY" != disabled ]]; then
  # Keep this raw capability out of argv, policy JSON, the model shell, and the
  # runner's globally exported environment. Only the owner-side adapter,
  # guardian, and review driver receive it on their process invocation. The
  # host derives a distinct revocable capability for each reasoning epoch; the
  # owner capability itself must never enter reasoning MCP config or process
  # environment.
  RETHLAS_REVIEW_CONTROL_TOKEN="$(
    "$TRUSTED_PYTHON_BIN" -I -B -c 'import secrets; print(secrets.token_hex(32))'
  )"
fi

# The publication tool intentionally uses a lossless, restricted identifier.
# Reject names that its path validator would otherwise normalize differently.
IFS='/' read -r -a problem_parts <<< "$problem_rel"
for component in "${problem_parts[@]}"; do
  if ! [[ "$component" =~ ^[A-Za-z0-9]([A-Za-z0-9._-]*[A-Za-z0-9-])?$ ]]; then
    echo "Unsupported problem path component '$component'. Use ASCII letters, digits, '.', '_', or '-'; do not use leading/trailing './_'." >&2
    exit 1
  fi
done

prepare_references() {
  local abs_ref_dir="$ROOT_DIR/$ref_dir"
  if [[ ! -d "$abs_ref_dir" ]]; then
    return
  fi

  local pdf_count=0
  while IFS= read -r -d '' pdf; do
    pdf_count=$((pdf_count + 1))
    if ! command -v pdftotext >/dev/null 2>&1; then
      echo "WARNING: found PDF references, but pdftotext is not installed; PDFs will be ignored." >&2
      return
    fi

    local rel_pdf="${pdf#"$abs_ref_dir"/}"
    local txt="$abs_ref_dir/.extracted/${rel_pdf%.pdf}.txt"
    mkdir -p "$(dirname "$txt")"
    if [[ ! -f "$txt" || "$pdf" -nt "$txt" ]]; then
      pdftotext -layout "$pdf" "$txt"
    fi
  done < <(find "$abs_ref_dir" -type f -iname '*.pdf' -not -path "$abs_ref_dir/.extracted/*" -print0)

  if [[ $pdf_count -gt 0 ]]; then
    ref_prompt="Use reference_dir=${ref_dir} if it exists. PDF references have been extracted to ${ref_dir}/.extracted; read those extracted .txt files instead of the PDFs."
  fi
}

format_duration() {
  local total="$1"
  printf "%02d:%02d:%02d" \
    $((total / 3600)) $(((total % 3600) / 60)) $((total % 60))
}

LOG_DIR="${LOG_DIR:-$ROOT_DIR/logs/$problem_rel/iter}"
verified_path="$ROOT_DIR/results/$problem_rel/blueprint_verified.md"
trusted_receipts_root="$(cd "$ROOT_DIR/.." && pwd -P)/.verification_receipts"
if [[ -n "${RETHLAS_RECEIPTS_ROOT:-}" && "$RETHLAS_RECEIPTS_ROOT" != "$trusted_receipts_root" ]]; then
  echo "RETHLAS_RECEIPTS_ROOT is fixed outside the generation workspace and cannot be overridden." >&2
  exit 1
fi
if [[ -L "$trusted_receipts_root" ]]; then
  echo "Trusted receipt root must not be a symlink: $trusted_receipts_root" >&2
  exit 1
fi
mkdir -p "$trusted_receipts_root"
export RETHLAS_RECEIPTS_ROOT="$trusted_receipts_root"
export RETHLAS_EXPECTED_PROBLEM_ID="$problem_rel"
export RETHLAS_EXPECTED_STATEMENT_SHA256="$(
  "$TRUSTED_PYTHON_BIN" -I -B -c 'import hashlib, pathlib, sys; print(hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest())' \
    "$ROOT_DIR/$PROBLEM_FILE"
)"
candidate_projection_dir=".claude_core_inputs/reference_candidates/${problem_rel}/${RETHLAS_EXPECTED_STATEMENT_SHA256}"
receipt_path="$RETHLAS_RECEIPTS_ROOT/$problem_rel.json"
mkdir -p "$LOG_DIR"

trusted_runtime_manifest() {
  local manifest_root="${1:-$ROOT_DIR}"
  local runner_source="${2:-$LEGACY_RUNNER_SOURCE}"
  local expected_runner_sha256="${3:-$OWNED_RUNNER_SHA256}"
  "$TRUSTED_PYTHON_BIN" -I -B - \
    "$manifest_root" "$runner_source" "$expected_runner_sha256" <<'PY'
import hashlib
import os
import stat
import sys
from pathlib import Path

if not sys.flags.isolated or not sys.dont_write_bytecode:
    raise SystemExit("trusted runtime manifest requires Python -I -B")

root = Path(sys.argv[1]).resolve(strict=True)
runner_source_argument = Path(sys.argv[2]).absolute()
runner_source = (
    runner_source_argument
    if str(runner_source_argument).startswith(("/proc/self/fd/", "/dev/fd/"))
    else runner_source_argument.resolve(strict=True)
)
expected_runner_sha256 = sys.argv[3]
if expected_runner_sha256 and (
    len(expected_runner_sha256) != 64
    or any(character not in "0123456789abcdef" for character in expected_runner_sha256)
):
    raise SystemExit("invalid expected runner SHA-256")
explicit = [
    (root / "AGENTS.legacy.md", Path("AGENTS.legacy.md")),
    (root / "requirements-math-research.txt", Path("requirements-math-research.txt")),
    (runner_source, Path("tests/run_legacy.sh")),
    (root / "mcp" / "__init__.py", Path("mcp/__init__.py")),
    (
        root / "mcp" / "publication_proof_context_v3.py",
        Path("mcp/publication_proof_context_v3.py"),
    ),
    (root / "mcp" / "proof_context.py", Path("mcp/proof_context.py")),
    (
        root / "mcp" / "legacy_verification_client.py",
        Path("mcp/legacy_verification_client.py"),
    ),
    (root / "mcp" / "legacy_server.py", Path("mcp/legacy_server.py")),
]
trees = [
    (root / ".codex", Path(".codex")),
    (root / ".agents", Path(".agents")),
]

def fail(message: str) -> None:
    print(f"unsafe trusted generation runtime: {message}", file=sys.stderr)
    raise SystemExit(2)


for current, directories, names in os.walk(root / "mcp", followlinks=False):
    current_path = Path(current)
    if "__pycache__" in directories:
        fail(
            "Python bytecode cache directory is forbidden: "
            + str(current_path / "__pycache__")
        )
    if any(name.endswith((".pyc", ".pyo")) for name in names):
        fail(f"Python bytecode is forbidden under: {current_path}")


entries: list[tuple[str, Path, Path, os.stat_result]] = []
for path, logical_path in explicit:
    try:
        metadata = (
            os.fstat(int(path.name))
            if logical_path == Path("tests/run_legacy.sh")
            and str(path).startswith(("/proc/self/fd/", "/dev/fd/"))
            else path.lstat()
        )
    except OSError as exc:
        fail(f"cannot inspect {path}: {exc}")
    if not stat.S_ISREG(metadata.st_mode):
        fail(f"expected a regular file: {path}")
    entries.append(("file", path, logical_path, metadata))

for tree, logical_root in trees:
    try:
        tree_metadata = tree.lstat()
    except OSError as exc:
        fail(f"cannot inspect {tree}: {exc}")
    if not stat.S_ISDIR(tree_metadata.st_mode):
        fail(f"expected a non-symlink directory: {tree}")
    entries.append(("directory", tree, logical_root, tree_metadata))

    for current, directories, names in os.walk(tree, followlinks=False):
        current_path = Path(current)
        directories.sort()
        names.sort()
        for name in list(directories):
            candidate = current_path / name
            if name == "__pycache__":
                fail(f"Python bytecode cache directory is forbidden: {candidate}")
            try:
                metadata = candidate.lstat()
            except OSError as exc:
                fail(f"cannot inspect {candidate}: {exc}")
            if not stat.S_ISDIR(metadata.st_mode):
                fail(f"directory entry is a symlink or special file: {candidate}")
            logical_path = logical_root / candidate.relative_to(tree)
            entries.append(("directory", candidate, logical_path, metadata))

        for name in names:
            candidate = current_path / name
            if name.endswith((".pyc", ".pyo")):
                fail(f"Python bytecode is forbidden: {candidate}")
            try:
                metadata = candidate.lstat()
            except OSError as exc:
                fail(f"cannot inspect {candidate}: {exc}")
            if not stat.S_ISREG(metadata.st_mode):
                fail(f"file entry is a symlink or special file: {candidate}")
            logical_path = logical_root / candidate.relative_to(tree)
            entries.append(("file", candidate, logical_path, metadata))

if len(entries) > 2000:
    fail("trusted runtime has more than 2000 filesystem entries")
total = 0
manifest = hashlib.sha256()
seen: set[Path] = set()
seen_logical: set[Path] = set()
for kind, path, logical_path, metadata in sorted(
    entries,
    key=lambda item: (str(item[2]), item[0]),
):
    if path in seen:
        fail(f"duplicate runtime entry: {path}")
    seen.add(path)
    if logical_path in seen_logical:
        fail(f"duplicate logical runtime entry: {logical_path}")
    seen_logical.add(logical_path)
    relative = str(logical_path).encode("utf-8")
    kind_bytes = kind.encode("ascii")
    manifest.update(len(kind_bytes).to_bytes(1, "big"))
    manifest.update(kind_bytes)
    manifest.update(len(relative).to_bytes(4, "big"))
    manifest.update(relative)
    manifest.update(stat.S_IMODE(metadata.st_mode).to_bytes(4, "big"))
    if kind == "file":
        if metadata.st_size > 8_000_000:
            fail(f"trusted runtime file exceeds 8 MB: {path}")
        total += metadata.st_size
        if total > 32_000_000:
            fail("trusted runtime files exceed 32 MB in total")
        digest = hashlib.sha256()
        descriptor_backed_runner = (
            logical_path == Path("tests/run_legacy.sh")
            and str(path).startswith(("/proc/self/fd/", "/dev/fd/"))
        )
        if descriptor_backed_runner:
            try:
                descriptor = int(path.name)
                opened = os.fstat(descriptor)
            except (OSError, ValueError) as exc:
                fail(f"cannot inspect owned legacy runner descriptor: {exc}")
            identity = (
                opened.st_dev,
                opened.st_ino,
                opened.st_size,
                opened.st_mtime_ns,
                stat.S_IMODE(opened.st_mode),
            )
            if identity != (
                metadata.st_dev,
                metadata.st_ino,
                metadata.st_size,
                metadata.st_mtime_ns,
                stat.S_IMODE(metadata.st_mode),
            ):
                fail("owned legacy runner descriptor identity changed")
            offset = 0
            while offset < opened.st_size:
                try:
                    chunk = os.pread(
                        descriptor,
                        min(65_536, opened.st_size - offset),
                        offset,
                    )
                except OSError as exc:
                    fail(f"cannot read owned legacy runner descriptor: {exc}")
                if not chunk:
                    fail("owned legacy runner descriptor produced a short read")
                digest.update(chunk)
                offset += len(chunk)
            after = os.fstat(descriptor)
            if identity != (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
                stat.S_IMODE(after.st_mode),
            ):
                fail("owned legacy runner descriptor changed during hashing")
        else:
            with path.open("rb") as handle:
                while chunk := handle.read(65536):
                    digest.update(chunk)
        if (
            logical_path == Path("tests/run_legacy.sh")
            and expected_runner_sha256
            and digest.hexdigest() != expected_runner_sha256
        ):
            fail("owned legacy runner differs from its pinned SHA-256")
        manifest.update(digest.digest())
print(manifest.hexdigest())
PY
}

TRUSTED_RUNTIME_MANIFEST="$(trusted_runtime_manifest)" || {
  echo "Could not establish the trusted generation runtime manifest." >&2
  exit 1
}
export RETHLAS_TRUSTED_RUNTIME_SHA256="$TRUSTED_RUNTIME_MANIFEST"

# Codex can restart a failed MCP server within one session. A before/after hash
# of the writable source tree alone cannot detect code that was changed,
# executed on restart, and restored. Pin every MCP start/restart to an exact
# snapshot outside the generation workspace instead.
trusted_runtime_parent="$ROOT_DIR/../.trusted_generation_runtime"
if [[ -L "$trusted_runtime_parent" ]]; then
  echo "Trusted runtime snapshot root must not be a symlink: $trusted_runtime_parent" >&2
  exit 1
fi
mkdir -p "$trusted_runtime_parent"
trusted_runtime_parent="$(cd "$trusted_runtime_parent" && pwd -P)"
trusted_runtime_dir="$(mktemp -d "$trusted_runtime_parent/legacy.XXXXXX")"
mkdir -p "$trusted_runtime_dir/tests" "$trusted_runtime_dir/mcp"
cp -p "$ROOT_DIR/AGENTS.legacy.md" "$trusted_runtime_dir/AGENTS.legacy.md"
cp -p "$ROOT_DIR/requirements-math-research.txt" \
  "$trusted_runtime_dir/requirements-math-research.txt"
if [[ -n "$OWNED_RUNNER_FD" ]]; then
  # Darwin's cp delegates regular-file copies to fcopyfile(3), which rejects
  # /dev/fd/N even when N names an inherited regular-file descriptor.  Read
  # the already authenticated descriptor positionally so the copy is portable
  # and independent of any shared Darwin file offset.
  "$TRUSTED_PYTHON_BIN" -I -S -B - \
    "$OWNED_RUNNER_FD" "$trusted_runtime_dir/tests/run_legacy.sh" \
    "$OWNED_RUNNER_SHA256" <<'PY'
import hashlib
import os
import stat
import sys

source_descriptor = int(sys.argv[1])
destination = os.fsencode(sys.argv[2])
expected_sha256 = sys.argv[3]
before = os.fstat(source_descriptor)
if (
    not stat.S_ISREG(before.st_mode)
    or before.st_size <= 0
    or before.st_size > 8_000_000
):
    raise SystemExit("owned legacy runner is not a bounded regular file")
identity = (
    before.st_dev,
    before.st_ino,
    before.st_size,
    before.st_mtime_ns,
    stat.S_IMODE(before.st_mode),
)
chunks = []
offset = 0
while offset < before.st_size:
    chunk = os.pread(source_descriptor, min(65_536, before.st_size - offset), offset)
    if not chunk:
        raise SystemExit("owned legacy runner produced a short positional read")
    chunks.append(chunk)
    offset += len(chunk)
raw = b"".join(chunks)
after = os.fstat(source_descriptor)
if identity != (
    after.st_dev,
    after.st_ino,
    after.st_size,
    after.st_mtime_ns,
    stat.S_IMODE(after.st_mode),
):
    raise SystemExit("owned legacy runner changed during snapshot copy")
if hashlib.sha256(raw).hexdigest() != expected_sha256:
    raise SystemExit("owned legacy runner differs from its pinned SHA-256")

flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
destination_descriptor = -1
try:
    destination_descriptor = os.open(destination, flags, identity[4])
    view = memoryview(raw)
    while view:
        written = os.write(destination_descriptor, view)
        if written <= 0:
            raise SystemExit("owned legacy runner snapshot copy made no progress")
        view = view[written:]
    os.fchmod(destination_descriptor, identity[4])
    os.fsync(destination_descriptor)
except BaseException:
    if destination_descriptor >= 0:
        os.close(destination_descriptor)
        destination_descriptor = -1
    try:
        os.unlink(destination)
    except FileNotFoundError:
        pass
    raise
finally:
    if destination_descriptor >= 0:
        os.close(destination_descriptor)
PY
else
  cp -p "$LEGACY_RUNNER_SOURCE" "$trusted_runtime_dir/tests/run_legacy.sh"
fi
cp -pR "$ROOT_DIR/.codex" "$trusted_runtime_dir/.codex"
cp -pR "$ROOT_DIR/.agents" "$trusted_runtime_dir/.agents"
for module in __init__.py publication_proof_context_v3.py proof_context.py legacy_verification_client.py legacy_server.py; do
  cp -p "$ROOT_DIR/mcp/$module" "$trusted_runtime_dir/mcp/$module"
done
SNAPSHOT_RUNTIME_MANIFEST="$(
  trusted_runtime_manifest "$trusted_runtime_dir" \
    "$trusted_runtime_dir/tests/run_legacy.sh"
)" || {
  echo "Could not attest the trusted generation runtime snapshot." >&2
  exit 1
}
if [[ "$SNAPSHOT_RUNTIME_MANIFEST" != "$TRUSTED_RUNTIME_MANIFEST" ]]; then
  echo "Trusted generation runtime changed while its snapshot was created." >&2
  exit 70
fi
if [[ -n "${RETHLAS_COHORT_RUNNER_CLOSURE_SHA256:-}" \
   && "$TRUSTED_RUNTIME_MANIFEST" != "$RETHLAS_COHORT_RUNNER_CLOSURE_SHA256" ]]; then
  echo "Cohort runner closure differs from its immutable intent." >&2
  exit 70
fi
unset RETHLAS_COHORT_RUNNER_CLOSURE_SHA256
chmod -R a-w "$trusted_runtime_dir"

# Every MCP start and automatic restart executes only bytes read from securely
# opened, content-attested files. A read-only pathname is not a trust anchor:
# its owner can chmod, replace, execute, and restore it between the wrapper's
# before/after manifest checks. Keep the loader itself in the immutable CLI
# config, read every local executable dependency before importing any of them,
# and never reopen a snapshot path for execution.
attest_snapshot_module() {
  "$TRUSTED_PYTHON_BIN" -I -B - "$1" "$2" <<'PY'
import hashlib
import os
import stat
import sys
from pathlib import Path


def read_exact(path: Path, *, require_read_only: bool) -> bytes:
    absolute = Path(os.path.abspath(os.fspath(path)))
    before = absolute.lstat()
    if (
        absolute.is_symlink()
        or not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or before.st_size < 1
        or before.st_size > 8_000_000
        or (require_read_only and stat.S_IMODE(before.st_mode) & 0o222)
    ):
        raise ValueError(f"unsafe attested module: {absolute}")
    descriptor = os.open(
        absolute,
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        opened = os.fstat(descriptor)
        identity = (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
            opened.st_mtime_ns,
        )
        if identity != (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ):
            raise ValueError(f"attested module changed during open: {absolute}")
        remaining = int(opened.st_size)
        chunks = []
        while remaining:
            chunk = os.read(descriptor, min(65_536, remaining))
            if not chunk:
                raise ValueError(f"short read of attested module: {absolute}")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise ValueError(f"attested module grew during read: {absolute}")
        after = os.fstat(descriptor)
        if identity != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise ValueError(f"attested module changed during read: {absolute}")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


try:
    source = read_exact(Path(sys.argv[1]), require_read_only=False)
    snapshot = read_exact(Path(sys.argv[2]), require_read_only=True)
    if source != snapshot:
        raise ValueError("snapshot module differs from authenticated source bytes")
except (OSError, RuntimeError, ValueError) as exc:
    print(f"trusted MCP module attestation failed: {exc}", file=sys.stderr)
    raise SystemExit(70)
print(hashlib.sha256(source).hexdigest())
PY
}

MCP_SERVER_PATH="$trusted_runtime_dir/mcp/legacy_server.py"
MCP_PUBLICATION_PROOF_CONTEXT_PATH="$trusted_runtime_dir/mcp/publication_proof_context_v3.py"
MCP_PROOF_CONTEXT_PATH="$trusted_runtime_dir/mcp/proof_context.py"
MCP_VERIFICATION_CLIENT_PATH="$trusted_runtime_dir/mcp/legacy_verification_client.py"
LEGACY_INSTRUCTIONS_PATH="$trusted_runtime_dir/AGENTS.legacy.md"
MCP_SERVER_SHA256="$(attest_snapshot_module "$ROOT_DIR/mcp/legacy_server.py" "$MCP_SERVER_PATH")" || exit 70
MCP_PUBLICATION_PROOF_CONTEXT_SHA256="$(attest_snapshot_module "$ROOT_DIR/mcp/publication_proof_context_v3.py" "$MCP_PUBLICATION_PROOF_CONTEXT_PATH")" || exit 70
MCP_PROOF_CONTEXT_SHA256="$(attest_snapshot_module "$ROOT_DIR/mcp/proof_context.py" "$MCP_PROOF_CONTEXT_PATH")" || exit 70
MCP_VERIFICATION_CLIENT_SHA256="$(attest_snapshot_module "$ROOT_DIR/mcp/legacy_verification_client.py" "$MCP_VERIFICATION_CLIENT_PATH")" || exit 70
LEGACY_INSTRUCTIONS_SHA256="$(attest_snapshot_module "$ROOT_DIR/AGENTS.legacy.md" "$LEGACY_INSTRUCTIONS_PATH")" || exit 70
if [[ "$(trusted_runtime_manifest)" != "$TRUSTED_RUNTIME_MANIFEST" ]]; then
  echo "Trusted generation runtime changed during executable-module attestation." >&2
  exit 70
fi

TRUSTED_LEGACY_INSTRUCTIONS_TOML="$({
  "$TRUSTED_PYTHON_BIN" -I -B - \
    "$LEGACY_INSTRUCTIONS_PATH" "$LEGACY_INSTRUCTIONS_SHA256" <<'PY'
import hashlib
import json
import os
import re
import stat
import sys
from pathlib import Path

path = Path(os.path.abspath(sys.argv[1]))
expected_sha256 = sys.argv[2]
if re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None:
    raise SystemExit("invalid Legacy instruction digest")
before = path.lstat()
if (
    not stat.S_ISREG(before.st_mode)
    or stat.S_IMODE(before.st_mode) & 0o222
    or before.st_nlink != 1
    or before.st_size < 1
    or before.st_size > 32_768
):
    raise SystemExit("Legacy instructions are not a bounded read-only file")
descriptor = os.open(
    path,
    os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
)
try:
    opened = os.fstat(descriptor)
    identity = (
        opened.st_dev,
        opened.st_ino,
        opened.st_size,
        opened.st_mtime_ns,
    )
    if identity != (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    ):
        raise ValueError("Legacy instructions changed during open")
    remaining = int(opened.st_size)
    chunks = []
    while remaining:
        chunk = os.read(descriptor, min(65_536, remaining))
        if not chunk:
            raise ValueError("Legacy instructions produced a short read")
        chunks.append(chunk)
        remaining -= len(chunk)
    if os.read(descriptor, 1):
        raise ValueError("Legacy instructions grew during read")
    after = os.fstat(descriptor)
    if identity != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise ValueError("Legacy instructions changed during read")
finally:
    os.close(descriptor)
raw = b"".join(chunks)
if hashlib.sha256(raw).hexdigest() != expected_sha256:
    raise SystemExit("Legacy instruction digest mismatch")
try:
    text = raw.decode("utf-8")
except UnicodeDecodeError as exc:
    raise SystemExit("Legacy instructions are not UTF-8") from exc
print(json.dumps(text))
PY
})" || {
  echo "Could not encode the attested Legacy instruction profile." >&2
  exit 70
}

TRUSTED_MCP_SECURE_LOADER="$(cat <<'PY'
import hashlib, hmac, importlib.util, json, os, re, stat, sys, types

EXPECTED = (
    "mcp.publication_proof_context_v3",
    "mcp.proof_context",
    "mcp.legacy_verification_client",
    "mcp.legacy_server",
)


def fail(message):
    print("trusted MCP secure-loader failed: " + message, file=sys.stderr)
    raise SystemExit(70)


def secure_read(path_value, expected_sha256):
    if (
        not isinstance(path_value, str)
        or not os.path.isabs(path_value)
        or "\x00" in path_value
        or re.fullmatch(r"[0-9a-f]{64}", expected_sha256 or "") is None
    ):
        fail("invalid module path or digest")
    path = os.path.abspath(path_value)
    try:
        before = os.lstat(path)
        if (
            not stat.S_ISREG(before.st_mode)
            or stat.S_IMODE(before.st_mode) & 0o222
            or before.st_nlink != 1
            or before.st_size < 1
            or before.st_size > 8_000_000
        ):
            fail("module is not a bounded read-only regular file")
        allowed_uids = {0, os.geteuid()}
        if before.st_uid not in allowed_uids:
            fail("module owner is not trusted")
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError as exc:
        fail("cannot securely open module: " + str(exc))
    try:
        opened = os.fstat(descriptor)
        identity = (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
            opened.st_mtime_ns,
        )
        if identity != (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ):
            fail("module changed during secure open")
        chunks = []
        remaining = int(opened.st_size)
        while remaining:
            chunk = os.read(descriptor, min(65_536, remaining))
            if not chunk:
                fail("module produced a short read")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            fail("module grew during secure read")
        after = os.fstat(descriptor)
        if identity != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            fail("module changed during secure read")
    finally:
        os.close(descriptor)
    raw = b"".join(chunks)
    if not hmac.compare_digest(hashlib.sha256(raw).hexdigest(), expected_sha256):
        fail("module SHA-256 mismatch")
    return path, raw


if not sys.flags.isolated or not sys.dont_write_bytecode:
    fail("Python must run with -I -B")
arguments = sys.argv[1:]
try:
    separator = arguments.index("--")
except ValueError:
    separator = len(arguments)
module_arguments = arguments[:separator]
entry_arguments = arguments[separator + 1 :] if separator < len(arguments) else []
if len(module_arguments) != 3 * len(EXPECTED):
    fail("module commitment argument count mismatch")
captured = {}
for index, expected_name in enumerate(EXPECTED):
    name, path, digest = module_arguments[index * 3 : index * 3 + 3]
    if name != expected_name:
        fail("module commitment order/name mismatch")
    captured[name] = secure_read(path, digest)


def install_package(name):
    if name in sys.modules:
        fail("trusted runtime package alias is already loaded")
    module = types.ModuleType(name)
    module.__package__ = name
    module.__path__ = []
    module.__loader__ = None
    module.__spec__ = importlib.util.spec_from_loader(
        name, loader=None, is_package=True
    )
    sys.modules[name] = module


def execute_module(source_name, runtime_name=None):
    path, raw = captured[source_name]
    name = source_name if runtime_name is None else runtime_name
    module = types.ModuleType(name)
    module.__file__ = path
    module.__package__ = name.rpartition(".")[0]
    module.__loader__ = None
    module.__spec__ = importlib.util.spec_from_loader(name, loader=None, origin=path)
    sys.modules[name] = module
    try:
        code = compile(raw, path, "exec", dont_inherit=True)
        exec(code, module.__dict__)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module


local_mcp_package = "_rethlas_generation_mcp"
install_package(local_mcp_package)
for module_name in EXPECTED[:-1]:
    runtime_name = (
        local_mcp_package + module_name[len("mcp") :]
        if module_name.startswith("mcp.")
        else module_name
    )
    execute_module(module_name, runtime_name)
verification_client = sys.modules[
    local_mcp_package + ".legacy_verification_client"
]
server = execute_module(
    "mcp.legacy_server", local_mcp_package + ".legacy_server"
)
if entry_arguments[:1] == ["--recover-prepared-publication"]:
    if len(entry_arguments) != 8:
        fail("prepared recovery argument count mismatch")
    (
        _mode,
        statement_path,
        draft_path,
        verified_path,
        receipt_path,
        problem_id,
        results_root,
        endpoint,
    ) = entry_arguments
    if not os.path.lexists(receipt_path):
        raise SystemExit(3)

    def bounded_regular_read(path_value, maximum):
        path = os.path.abspath(path_value)
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > maximum:
                fail("prepared recovery input is unsafe or over limit")
            chunks = []
            remaining = int(metadata.st_size)
            while remaining:
                chunk = os.read(descriptor, min(65_536, remaining))
                if not chunk:
                    fail("prepared recovery input produced a short read")
                chunks.append(chunk)
                remaining -= len(chunk)
            if os.read(descriptor, 1):
                fail("prepared recovery input grew while read")
            return b"".join(chunks)
        finally:
            os.close(descriptor)

    try:
        statement = bounded_regular_read(statement_path, 16_000_000).decode("utf-8")
        receipt = json.loads(
            bounded_regular_read(receipt_path, 64_000_000).decode("utf-8")
        )
        supersedes = receipt.get("supersedes", [])
        result = verification_client.verify_blueprint_file(
            statement=statement,
            draft_path=verification_client.Path(draft_path),
            verified_path=verification_client.Path(verified_path),
            endpoint=endpoint,
            timeout_seconds=120,
            receipt_path=verification_client.Path(receipt_path),
            problem_id=problem_id,
            blueprint_root=verification_client.Path(results_root),
            verification_quorum=2,
            supersedes=supersedes,
            verification_profile=os.environ.get(
                "RETHLAS_MODEL_POLICY_PROFILE", "compatible"
            ),
            prepared_only=True,
        )
    except (OSError, RuntimeError, TypeError, UnicodeError, ValueError) as exc:
        fail("prepared publication recovery failed: " + str(exc))
    print(json.dumps(result, allow_nan=False, sort_keys=True, separators=(",", ":")))
    raise SystemExit(0)
sys.argv = [captured["mcp.legacy_server"][0], *entry_arguments]
server.main()
PY
)"
TRUSTED_MCP_LOADER_ARGS=(
  -I -B -c "$TRUSTED_MCP_SECURE_LOADER"
  mcp.publication_proof_context_v3 "$MCP_PUBLICATION_PROOF_CONTEXT_PATH" "$MCP_PUBLICATION_PROOF_CONTEXT_SHA256"
  mcp.proof_context "$MCP_PROOF_CONTEXT_PATH" "$MCP_PROOF_CONTEXT_SHA256"
  mcp.legacy_verification_client "$MCP_VERIFICATION_CLIENT_PATH" "$MCP_VERIFICATION_CLIENT_SHA256"
  mcp.legacy_server "$MCP_SERVER_PATH" "$MCP_SERVER_SHA256"
)
export RETHLAS_GENERATION_ROOT="$ROOT_DIR"
export RETHLAS_RUNTIME_PROFILE="legacy"
TRUSTED_PYTHON_COMMAND_TOML="$(
  "$TRUSTED_PYTHON_BIN" -I -B -c 'import json, sys; print(json.dumps(sys.argv[1]))' \
    "$trusted_python_command"
)"
TRUSTED_MCP_ARGS_TOML="$(
  "$TRUSTED_PYTHON_BIN" -I -B -c 'import json, sys; print(json.dumps(sys.argv[1:]))' \
    "${TRUSTED_MCP_LOADER_ARGS[@]}"
)"
TRUSTED_MCP_CWD_TOML="$(
  "$TRUSTED_PYTHON_BIN" -I -B -c 'import json, sys; print(json.dumps(sys.argv[1]))' "$ROOT_DIR"
)"

trusted_runtime_unchanged() {
  local current_manifest
  current_manifest="$(trusted_runtime_manifest)" || return 1
  [[ "$current_manifest" == "$TRUSTED_RUNTIME_MANIFEST" ]]
}

generation_control_resume() {
  RETHLAS_GENERATION_CONTROL_TOKEN="$LEGACY_GENERATION_CONTROL_TOKEN" \
    "$TRUSTED_PYTHON_BIN" "${TRUSTED_MCP_LOADER_ARGS[@]}" -- \
      --generation-control-resume "$problem_rel"
}

generation_control_receipt() {
  RETHLAS_GENERATION_CONTROL_TOKEN="$LEGACY_GENERATION_CONTROL_TOKEN" \
    "$TRUSTED_PYTHON_BIN" "${TRUSTED_MCP_LOADER_ARGS[@]}" -- \
      --generation-control-receipt "$problem_rel"
}

legacy_frontier_receipt() {
  "$TRUSTED_PYTHON_BIN" "${TRUSTED_MCP_LOADER_ARGS[@]}" -- \
    --legacy-frontier-receipt "$problem_rel"
}

legacy_frontier_sha_from_receipt() {
  local receipt="$1"
  RETHLAS_LEGACY_FRONTIER_RECEIPT_JSON="$receipt" \
    "$TRUSTED_PYTHON_BIN" -I -B - \
      "$problem_rel" "$RETHLAS_EXPECTED_STATEMENT_SHA256" <<'PY'
import hashlib
import json
import os
import re
import sys

try:
    receipt = json.loads(os.environ["RETHLAS_LEGACY_FRONTIER_RECEIPT_JSON"])
except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
    raise SystemExit(f"invalid Legacy frontier receipt JSON: {exc}")
keys = {
    "schema_version",
    "problem_id",
    "statement_sha256",
    "blueprint",
    "memory_sha256",
    "memory_record_count",
    "frontier_sha256",
}
sha = re.compile(r"[0-9a-f]{64}")
if (
    not isinstance(receipt, dict)
    or set(receipt) != keys
    or receipt.get("schema_version") != "rethlas_legacy_frontier_receipt_v1"
    or receipt.get("problem_id") != sys.argv[1]
    or receipt.get("statement_sha256") != sys.argv[2]
    or not isinstance(receipt.get("statement_sha256"), str)
    or sha.fullmatch(receipt["statement_sha256"]) is None
    or not isinstance(receipt.get("memory_sha256"), str)
    or sha.fullmatch(receipt["memory_sha256"]) is None
    or not isinstance(receipt.get("frontier_sha256"), str)
    or sha.fullmatch(receipt["frontier_sha256"]) is None
    or isinstance(receipt.get("memory_record_count"), bool)
    or not isinstance(receipt.get("memory_record_count"), int)
    or receipt["memory_record_count"] < 0
):
    raise SystemExit("invalid Legacy frontier receipt envelope")
blueprint = receipt["blueprint"]
if blueprint is not None and (
    not isinstance(blueprint, dict)
    or set(blueprint) != {"sha256", "bytes"}
    or not isinstance(blueprint.get("sha256"), str)
    or sha.fullmatch(blueprint["sha256"]) is None
    or isinstance(blueprint.get("bytes"), bool)
    or not isinstance(blueprint.get("bytes"), int)
    or not 0 < blueprint["bytes"] <= 8_000_000
):
    raise SystemExit("invalid Legacy frontier blueprint commitment")
body = dict(receipt)
claimed = body.pop("frontier_sha256")
encoded = json.dumps(
    body,
    allow_nan=False,
    ensure_ascii=False,
    sort_keys=True,
    separators=(",", ":"),
).encode("utf-8")
if hashlib.sha256(encoded).hexdigest() != claimed:
    raise SystemExit("Legacy frontier digest mismatch")
print(claimed)
PY
}

generation_control_state_from_receipt() {
  local receipt="$1"
  RETHLAS_GENERATION_CONTROL_RECEIPT_JSON="$receipt" \
    "$TRUSTED_PYTHON_BIN" -I -B -c \
      'import json, os; print(json.loads(os.environ["RETHLAS_GENERATION_CONTROL_RECEIPT_JSON"])["control"]["state"])'
}

receipt_is_valid() {
  "$TRUSTED_PYTHON_BIN" -I -B - "$ROOT_DIR" "$receipt_path" "$verified_path" "$problem_rel" \
    "$RETHLAS_EXPECTED_STATEMENT_SHA256" "$ROOT_DIR/$PROBLEM_FILE" \
    "$MCP_PUBLICATION_PROOF_CONTEXT_PATH" "$MCP_PUBLICATION_PROOF_CONTEXT_SHA256" <<'PY'
import hashlib
import hmac
import json
import os
import re
import stat
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

if not sys.flags.isolated or not sys.dont_write_bytecode:
    raise SystemExit("publication receipt validation requires Python -I -B")

root = Path(sys.argv[1]).absolute()
receipt_path = Path(sys.argv[2]).absolute()
verified_path = Path(sys.argv[3]).absolute()
problem_id = sys.argv[4]
statement_digest = sys.argv[5]
problem_path = Path(sys.argv[6]).absolute()
proof_context_path = Path(sys.argv[7]).absolute()
proof_context_sha256 = sys.argv[8]
receipt_root = root.parent / ".verification_receipts"
results_root = root / "results"
max_receipt_bytes = 64_000_000
max_blueprint_bytes = 64_000_000
max_module_bytes = 8_000_000

def open_parent(root_path: Path, parts: list[str]) -> int:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(root_path, flags)
    try:
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise ValueError("unsafe root")
        for part in parts:
            next_descriptor = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
            if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
                raise ValueError("unsafe parent")
        return descriptor
    except Exception:
        os.close(descriptor)
        raise

def bounded_regular_bytes(parent: Path, relative_parent: list[str], name: str, limit: int) -> bytes:
    parent_fd = open_parent(parent, relative_parent)
    descriptor = -1
    try:
        descriptor = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > limit:
            raise ValueError("unsafe or oversized file")
        chunks = []
        remaining = limit + 1
        while remaining:
            chunk = os.read(descriptor, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) > limit:
            raise ValueError("oversized file")
        return raw
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(parent_fd)

def load_attested_proof_context():
    snapshot_root = root.parent / ".trusted_generation_runtime"
    try:
        proof_context_path.resolve(strict=True).relative_to(snapshot_root.resolve(strict=True))
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError("proof_context is not inside the trusted runtime snapshot") from exc
    before = proof_context_path.lstat()
    if (
        proof_context_path.is_symlink()
        or not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or before.st_size < 1
        or before.st_size > max_module_bytes
        or stat.S_IMODE(before.st_mode) & 0o222
        or (hasattr(os, "getuid") and before.st_uid not in {0, os.getuid()})
    ):
        raise ValueError("proof_context snapshot is not a bounded read-only regular file")
    descriptor = os.open(
        proof_context_path,
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        opened = os.fstat(descriptor)
        identity = (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
        if identity != (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns):
            raise ValueError("proof_context snapshot changed during secure open")
        chunks = []
        remaining = int(opened.st_size)
        while remaining:
            chunk = os.read(descriptor, min(65_536, remaining))
            if not chunk:
                raise ValueError("proof_context snapshot produced a short read")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise ValueError("proof_context snapshot grew during secure read")
        after = os.fstat(descriptor)
        if identity != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            raise ValueError("proof_context snapshot changed during secure read")
    finally:
        os.close(descriptor)
    raw = b"".join(chunks)
    if not hmac.compare_digest(hashlib.sha256(raw).hexdigest(), proof_context_sha256):
        raise ValueError("proof_context snapshot digest mismatch")
    module_name = "_rethlas_receipt_proof_context"
    module = types.ModuleType(module_name)
    module.__file__ = str(proof_context_path)
    module.__package__ = ""
    sys.modules[module_name] = module
    try:
        exec(
            compile(raw, str(proof_context_path), "exec", dont_inherit=True),
            module.__dict__,
        )
    except BaseException:
        sys.modules.pop(module_name, None)
        raise
    return module

try:
    components = problem_id.split("/")
    if not components or any(
        re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9-])?", part) is None
        for part in components
    ):
        raise ValueError("unsafe problem id")
    if receipt_path != receipt_root.joinpath(*components[:-1], components[-1] + ".json"):
        raise ValueError("wrong receipt path")
    if verified_path != results_root.joinpath(*components, "blueprint_verified.md"):
        raise ValueError("wrong verified path")
    receipt_raw = bounded_regular_bytes(
        receipt_root, components[:-1], components[-1] + ".json", max_receipt_bytes
    )
    verified_raw = bounded_regular_bytes(
        results_root, components, "blueprint_verified.md", max_blueprint_bytes
    )
    problem_raw = problem_path.read_bytes()
    if hashlib.sha256(problem_raw).hexdigest() != statement_digest:
        raise ValueError("problem changed")
    receipt = json.loads(receipt_raw.decode("utf-8"))
    v2_keys = {
        "schema_version", "problem_id", "statement_digest", "proof_digest",
        "context_digest", "adaptive_context_digest", "item_context_attestations",
        "checked_item_ids", "verified_path", "published_bytes",
    }
    v3_keys = {
        "schema_version", "state", "problem_id", "statement_source_digest",
        "canonical_target_digest", "proof_digest", "context_digest",
        "adaptive_context_digest", "item_context_attestations",
        "checked_item_ids", "verified_path", "published_bytes",
        "published_at_utc", "verification_quorum", "verification_passes",
        "supersedes",
    }
    v4_keys = v3_keys | {"proof_context", "verification_limits"}
    v5_keys = v4_keys | {"publication_target_precondition"}
    if not isinstance(receipt, dict):
        raise ValueError("invalid receipt shape")
    schema = receipt.get("schema_version")
    if not (
        (schema == "rethlas-publication-v2" and set(receipt) == v2_keys)
        or (schema == "rethlas-publication-v3" and set(receipt) == v3_keys)
        or (schema == "rethlas-publication-v4" and set(receipt) == v4_keys)
        or (schema == "rethlas-publication-v5" and set(receipt) == v5_keys)
        or (schema == "rethlas-publication-v6" and set(receipt) == v5_keys)
    ):
        raise ValueError("invalid receipt version")
    if schema != "rethlas-publication-v6" and len(verified_raw) > 8_000_000:
        raise ValueError("legacy publication blueprint exceeds its fixed byte cap")
    context_max_chars = 64_000_000
    max_expansion_rounds = 4_096
    max_expanded_proofs = 100_000
    max_expanded_proof_chars = 64_000_000
    max_proof_items = None
    if schema in {
        "rethlas-publication-v4", "rethlas-publication-v5",
        "rethlas-publication-v6",
    }:
        limits = receipt["verification_limits"]
        limit_keys = {
            "context_max_chars", "max_expansion_rounds", "max_expanded_proofs",
            "max_expanded_proof_chars", "max_proof_items", "max_receipt_bytes",
        }
        absolutes = {
            "context_max_chars": 64_000_000,
            "max_expansion_rounds": 4_096,
            "max_expanded_proofs": 100_000,
            "max_expanded_proof_chars": 64_000_000,
            "max_proof_items": 100_000,
            "max_receipt_bytes": 64_000_000,
        }
        if schema == "rethlas-publication-v6":
            limit_keys |= {"max_blueprint_bytes", "max_blueprint_chars"}
            absolutes.update(
                {
                    "max_blueprint_bytes": 64_000_000,
                    "max_blueprint_chars": 16_000_000,
                }
            )
        if not isinstance(limits, dict) or set(limits) != limit_keys:
            raise ValueError("invalid v4 publication limits")
        for field, absolute in absolutes.items():
            value = limits[field]
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value < 0
                or value > absolute
            ):
                raise ValueError("invalid v4 publication limits")
        if (
            limits["max_proof_items"] <= 0
            or limits["max_receipt_bytes"] <= 0
            or len(receipt_raw) > limits["max_receipt_bytes"]
            or (
                schema == "rethlas-publication-v6"
                and (
                    limits["max_blueprint_bytes"] <= 0
                    or limits["max_blueprint_chars"] <= 0
                    or len(verified_raw) > limits["max_blueprint_bytes"]
                    or len(verified_raw.decode("utf-8"))
                    > limits["max_blueprint_chars"]
                )
            )
        ):
            raise ValueError("v4 publication exceeds its persisted limits")
        context_max_chars = limits["context_max_chars"]
        max_expansion_rounds = limits["max_expansion_rounds"]
        max_expanded_proofs = limits["max_expanded_proofs"]
        max_expanded_proof_chars = limits["max_expanded_proof_chars"]
        max_proof_items = limits["max_proof_items"]
        proof_context_binding = receipt["proof_context"]
        if (
            not isinstance(proof_context_binding, dict)
            or set(proof_context_binding) != {
                "schema_version", "source_sha256", "proof_item_schema_version",
                "proof_context_schema_version", "aggregate_context_schema_version",
                "adaptive_aggregate_context_schema_version",
            }
            or proof_context_binding["schema_version"]
            != "rethlas_publication_proof_context_v3"
            or proof_context_binding["source_sha256"] != proof_context_sha256
            or proof_context_binding["proof_item_schema_version"] != 1
            or proof_context_binding["proof_context_schema_version"] != 2
            or proof_context_binding["aggregate_context_schema_version"] != 1
            or proof_context_binding["adaptive_aggregate_context_schema_version"]
            != 2
        ):
            raise ValueError("invalid v4 proof-context binding")
    if schema in {"rethlas-publication-v5", "rethlas-publication-v6"}:
        target = receipt["publication_target_precondition"]
        target_fields = {
            "kind", "st_dev", "st_ino", "st_size", "st_mtime_ns",
            "content_sha256",
        }
        if not isinstance(target, dict) or set(target) != target_fields:
            raise ValueError("invalid v5 publication target precondition")
        bindings = (
            target["st_dev"], target["st_ino"], target["st_size"],
            target["st_mtime_ns"], target["content_sha256"],
        )
        if target["kind"] == "absent":
            if bindings != (None, None, None, None, None):
                raise ValueError("invalid v5 absent target precondition")
        elif target["kind"] in {"regular", "symlink"}:
            if (
                any(
                    isinstance(value, bool)
                    or not isinstance(value, int)
                    or value < 0
                    for value in bindings[:4]
                )
                or not isinstance(bindings[4], str)
                or re.fullmatch(r"[0-9a-f]{64}", bindings[4]) is None
            ):
                raise ValueError("invalid v5 target precondition binding")
        else:
            raise ValueError("invalid v5 publication target kind")
    if receipt["problem_id"] != problem_id:
        raise ValueError("wrong problem")
    if schema == "rethlas-publication-v2":
        if receipt["statement_digest"] != statement_digest:
            raise ValueError("stale statement")
    else:
        if (
            receipt["state"] != "active"
            or receipt["statement_source_digest"] != statement_digest
            or receipt["verification_quorum"] != 2
            or not isinstance(receipt["verification_passes"], list)
            or len(receipt["verification_passes"]) != 2
            or not isinstance(receipt["supersedes"], list)
            or len(receipt["supersedes"]) > 1
        ):
            raise ValueError("invalid v3 publication binding")
        published = datetime.fromisoformat(receipt["published_at_utc"])
        if (
            published.tzinfo is None
            or published.utcoffset() != timedelta(0)
            or receipt["published_at_utc"]
            != published.astimezone(timezone.utc).isoformat()
        ):
            raise ValueError("noncanonical publication timestamp")
        attempts = []
        runs = []
        pass_fields = {
            "pass_index", "verification_attempt_id", "verifier_run_id",
            "verifier_model", "verifier_reasoning_effort",
            "verifier_service_version", "verification_role",
            "response_sha256", "verdict",
        }
        efforts = {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}
        for index, verification_pass in enumerate(receipt["verification_passes"], 1):
            expected_role = "primary" if index == 1 else "adversarial_full_claim_audit"
            if (
                not isinstance(verification_pass, dict)
                or set(verification_pass) != pass_fields
                or isinstance(verification_pass["pass_index"], bool)
                or not isinstance(verification_pass["pass_index"], int)
                or verification_pass["pass_index"] != index
                or not isinstance(verification_pass["verification_attempt_id"], str)
                or re.fullmatch(r"veratt_[0-9a-f]{32}", verification_pass["verification_attempt_id"]) is None
                or not isinstance(verification_pass["verifier_run_id"], str)
                or not verification_pass["verifier_run_id"]
                or not isinstance(verification_pass["verifier_model"], str)
                or not verification_pass["verifier_model"]
                or not isinstance(verification_pass["verifier_reasoning_effort"], str)
                or verification_pass["verifier_reasoning_effort"] not in efforts
                or not isinstance(verification_pass["verifier_service_version"], str)
                or not verification_pass["verifier_service_version"]
                or verification_pass["verification_role"] != expected_role
                or not isinstance(verification_pass["response_sha256"], str)
                or re.fullmatch(r"[0-9a-f]{64}", verification_pass["response_sha256"]) is None
                or verification_pass["verdict"] != "correct"
            ):
                raise ValueError("invalid verifier quorum pass")
            attempts.append(verification_pass["verification_attempt_id"])
            runs.append(verification_pass["verifier_run_id"])
        if len(set(attempts)) != 2 or len(set(runs)) != 2:
            raise ValueError("verifier quorum is not independent")
        for superseded in receipt["supersedes"]:
            if (
                not isinstance(superseded, dict)
                or set(superseded) != {"problem_id", "receipt_sha256", "proof_digest"}
                or not isinstance(superseded["problem_id"], str)
                or not superseded["problem_id"]
                or re.fullmatch(r"[0-9a-f]{64}", superseded["receipt_sha256"]) is None
                or re.fullmatch(r"[0-9a-f]{64}", superseded["proof_digest"]) is None
            ):
                raise ValueError("invalid superseded publication binding")
    if receipt["proof_digest"] != hashlib.sha256(verified_raw).hexdigest():
        raise ValueError("verified bytes changed")
    if isinstance(receipt["published_bytes"], bool) or receipt["published_bytes"] != len(verified_raw):
        raise ValueError("verified byte count changed")
    if receipt["verified_path"] != str(verified_path):
        raise ValueError("wrong verified path")
    ids = receipt["checked_item_ids"]
    if (
        not isinstance(ids, list)
        or not ids
        or (max_proof_items is not None and len(ids) > max_proof_items)
        or len(set(ids)) != len(ids)
        or any(
            not isinstance(item_id, str)
            or re.fullmatch(r"pi_[0-9a-f]{24}", item_id) is None
            for item_id in ids
        )
    ):
        raise ValueError("invalid item coverage")
    if re.fullmatch(r"[0-9a-f]{64}", receipt["context_digest"]) is None:
        raise ValueError("invalid context digest")
    proof_context = load_attested_proof_context()
    aggregate_adaptive_context_digest = proof_context.aggregate_adaptive_context_digest
    aggregate_context_digest = proof_context.aggregate_context_digest
    build_item_context = proof_context.build_item_context
    extract_verification_target = proof_context.extract_verification_target
    parse_blueprint = proof_context.parse_blueprint
    proof_text = verified_raw.decode("utf-8")
    statement_text = problem_raw.decode("utf-8")
    manifest = parse_blueprint(proof_text, target_statement=statement_text)
    if schema != "rethlas-publication-v2" and receipt[
        "canonical_target_digest"
    ] != hashlib.sha256(
        extract_verification_target(statement_text).encode("utf-8")
    ).hexdigest():
        raise ValueError("canonical target digest mismatch")
    if ids != list(manifest.item_ids):
        raise ValueError("receipt item coverage does not match verified blueprint")
    if receipt["context_digest"] != aggregate_context_digest(manifest):
        raise ValueError("receipt context digest does not match verified blueprint")
    attestations = receipt["item_context_attestations"]
    if not isinstance(attestations, list) or len(attestations) != len(ids):
        raise ValueError("invalid adaptive context coverage")
    fields = {
        "item_id", "disposition", "final_round", "expanded_proof_ids",
        "max_chars", "context_digest", "verdict",
    }
    for item_id, record in zip(ids, attestations, strict=True):
        if not isinstance(record, dict) or set(record) != fields:
            raise ValueError("invalid item context attestation shape")
        expanded_ids = record["expanded_proof_ids"]
        if (
            record["item_id"] != item_id
            or record["disposition"] != "verified"
            or record["verdict"] != "correct"
            or isinstance(record["final_round"], bool)
            or not isinstance(record["final_round"], int)
            or not 0 <= record["final_round"] <= max_expansion_rounds
            or not isinstance(expanded_ids, list)
            or len(expanded_ids) > max_expanded_proofs
            or len(set(expanded_ids)) != len(expanded_ids)
            or any(
                not isinstance(expanded_id, str)
                or re.fullmatch(r"pi_[0-9a-f]{24}", expanded_id) is None
                for expanded_id in expanded_ids
            )
            or (record["final_round"] == 0) != (expanded_ids == [])
            or isinstance(record["max_chars"], bool)
            or not isinstance(record["max_chars"], int)
            or not 0 < record["max_chars"] <= context_max_chars
            or not isinstance(record["context_digest"], str)
            or re.fullmatch(r"[0-9a-f]{64}", record["context_digest"]) is None
        ):
            raise ValueError("item context attestation order mismatch")
        rebuilt = build_item_context(
            manifest,
            item_id,
            max_chars=record["max_chars"],
            expanded_proof_ids=record["expanded_proof_ids"],
            round_index=record["final_round"],
        )
        if (
            rebuilt["complete"] is not True
            or rebuilt["truncated"] is not False
            or rebuilt["missing"]
            or rebuilt["omitted"]
            or rebuilt["digest"] != record["context_digest"]
            or rebuilt["expanded_proof_characters"] > max_expanded_proof_chars
        ):
            raise ValueError("item context attestation mismatch")
    if receipt["adaptive_context_digest"] != aggregate_adaptive_context_digest(
        manifest, attestations
    ):
        raise ValueError("adaptive context digest mismatch")
    if os.environ.get("RETHLAS_EMIT_COMPLETION_EVIDENCE") == "1":
        print(json.dumps(
            {
                "schema_version": "rethlas_continuous_completion_evidence_v1",
                "problem_id": problem_id,
                "statement_sha256": statement_digest,
                "publication_receipt_sha256": hashlib.sha256(
                    receipt_raw
                ).hexdigest(),
                "verified_proof_sha256": hashlib.sha256(
                    verified_raw
                ).hexdigest(),
                "published_bytes": len(verified_raw),
            },
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ))
except (OSError, RuntimeError, ValueError, TypeError, UnicodeError, json.JSONDecodeError, ImportError) as exc:
    if os.environ.get("RETHLAS_RECEIPT_DEBUG") == "1":
        print(f"invalid publication receipt: {exc}", file=sys.stderr)
    raise SystemExit(1)
PY
}

if [[ -e "$verified_path" || -L "$verified_path" ]]; then
  if receipt_is_valid; then
    echo "Existing verified blueprint has a valid publication receipt."
    exit 0
  fi
  echo "Ignoring untrusted/stale verified blueprint at $verified_path"
fi

# A v4/v5 verifier receipt is itself the durable prepare record for the
# cross-directory publication. Recover it locally before any control, Codex,
# or verifier capability check; prepared_only makes network dispatch
# structurally unreachable inside the attested client.
if [[ -e "$receipt_path" || -L "$receipt_path" ]]; then
  set +e
  prepared_recovery_result="$({
    "$TRUSTED_PYTHON_BIN" "${TRUSTED_MCP_LOADER_ARGS[@]}" -- \
      --recover-prepared-publication \
      "$ROOT_DIR/$PROBLEM_FILE" \
      "$ROOT_DIR/results/$problem_rel/blueprint.md" \
      "$verified_path" \
      "$receipt_path" \
      "$problem_rel" \
      "$ROOT_DIR/results" \
      "https://prepared-recovery.invalid/verify"
  })"
  prepared_recovery_rc=$?
  set -e
  if [[ "$prepared_recovery_rc" -ne 0 ]]; then
    echo "Prepared publication recovery failed closed." >&2
    exit 70
  fi
  echo "Prepared publication recovery: $prepared_recovery_result"
  if receipt_is_valid; then
    echo "Recovered verified blueprint from durable verifier evidence."
    exit 0
  fi
fi

prepare_references

if ! generation_control_resume; then
  echo "Could not initialize isolated legacy generation control." >&2
  exit 70
fi

cohort_codex_fd_mode=0
if [[ -n "$COHORT_CODEX_FD" ]]; then
  if [[ ! "$COHORT_CODEX_FD" =~ ^[0-9]+$ \
     || "$COHORT_CODEX_FD" -lt 3 \
     || -z "${RETHLAS_COHORT_CODEX_BIN:-}" \
     || -z "${RETHLAS_COHORT_CODEX_SHA256:-}" \
     || ! -r "$descriptor_root/$COHORT_CODEX_FD" ]]; then
    echo "Bound cohort Codex descriptor is invalid." >&2
    exit 70
  fi
  if [[ "$descriptor_execution_supported" == 1 ]]; then
    if [[ ! -x "$descriptor_root/$COHORT_CODEX_FD" ]]; then
      echo "Bound cohort Codex descriptor is not executable." >&2
      exit 70
    fi
    codex_command="$descriptor_root/$COHORT_CODEX_FD"
  else
    codex_command="$RETHLAS_COHORT_CODEX_BIN"
  fi
  cohort_codex_fd_mode=1
elif [[ -n "${RETHLAS_COHORT_CODEX_BIN:-}" ]]; then
  if [[ -z "${RETHLAS_COHORT_CODEX_SHA256:-}" ]]; then
    echo "Bound cohort Codex path lacks its SHA-256." >&2
    exit 70
  fi
  codex_command="$RETHLAS_COHORT_CODEX_BIN"
elif [[ -n "${RETHLAS_COHORT_CODEX_SHA256:-}" ]]; then
  echo "Bound cohort Codex SHA-256 lacks its path." >&2
  exit 70
else
  codex_command="$(command -v codex || true)"
fi
if [[ "$codex_command" != /* ]] || [[ ! -x "$codex_command" ]]; then
  echo "codex must resolve to an absolute executable path." >&2
  exit 1
fi
attest_codex_binary() {
  "$TRUSTED_PYTHON_BIN" -I -B - "$1" <<'PY'
import hashlib
import json
import os
import re
import stat
import sys
from pathlib import Path


def fail(message: str) -> None:
    print(f"unsafe Codex executable: {message}", file=sys.stderr)
    raise SystemExit(1)


argument = os.path.abspath(sys.argv[1])
descriptor_match = re.fullmatch(r"/(?:proc/self|dev)/fd/([0-9]+)", argument)
try:
    if descriptor_match is not None:
        inherited_descriptor = int(descriptor_match.group(1))
        target = Path(argument)
        before = os.fstat(inherited_descriptor)
        descriptor = os.dup(inherited_descriptor)
    else:
        source = Path(argument)
        target = source.resolve(strict=True)
        before = target.lstat()
        if target.is_symlink():
            fail("resolved target must be a regular non-symlink file")
        flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        descriptor = os.open(target, flags)
except (OSError, RuntimeError) as exc:
    fail(f"cannot resolve executable: {exc}")
if not stat.S_ISREG(before.st_mode):
    fail("resolved target must be a regular file")
if stat.S_IMODE(before.st_mode) & 0o022:
    fail("resolved target must not be group/world-writable")
allowed_uids = {0}
if hasattr(os, "geteuid"):
    allowed_uids.add(os.geteuid())
if hasattr(before, "st_uid") and before.st_uid not in allowed_uids:
    fail("resolved target must be owned by the current owner or root")
if stat.S_IMODE(before.st_mode) & 0o111 == 0:
    fail("resolved target is not executable")
try:
    opened = os.fstat(descriptor)
    if not stat.S_ISREG(opened.st_mode) or (
        opened.st_dev,
        opened.st_ino,
        opened.st_size,
        opened.st_mtime_ns,
    ) != (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    ):
        fail("resolved target changed while opened")
    digest = hashlib.sha256()
    offset = 0
    while chunk := os.pread(descriptor, 65536, offset):
        digest.update(chunk)
        offset += len(chunk)
    after = os.fstat(descriptor)
    if (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ) != (
        opened.st_dev,
        opened.st_ino,
        opened.st_size,
        opened.st_mtime_ns,
    ):
        fail("resolved target changed while hashed")
finally:
    os.close(descriptor)
print(json.dumps(
    {"resolved_path": str(target), "sha256": digest.hexdigest()},
    sort_keys=True,
    separators=(",", ":"),
))
PY
}
CODEX_ATTESTATION="$(attest_codex_binary "$codex_command")" || exit 1
CODEX_BIN="$({
  RETHLAS_CODEX_ATTESTATION_JSON="$CODEX_ATTESTATION" \
    "$TRUSTED_PYTHON_BIN" -I -B -c \
      'import json, os; print(json.loads(os.environ["RETHLAS_CODEX_ATTESTATION_JSON"])["resolved_path"])'
})"
CODEX_BIN_SHA256="$({
  RETHLAS_CODEX_ATTESTATION_JSON="$CODEX_ATTESTATION" \
    "$TRUSTED_PYTHON_BIN" -I -B -c \
      'import json, os; print(json.loads(os.environ["RETHLAS_CODEX_ATTESTATION_JSON"])["sha256"])'
})"
if [[ "$cohort_codex_fd_mode" == 1 \
   && "$CODEX_BIN_SHA256" != "$RETHLAS_COHORT_CODEX_SHA256" ]]; then
  echo "Cohort Codex descriptor differs from its immutable intent." >&2
  exit 70
fi
if [[ "$cohort_codex_fd_mode" == 0 \
   && -n "${RETHLAS_COHORT_CODEX_BIN:-}" ]] \
   && { [[ "$CODEX_BIN" != "$RETHLAS_COHORT_CODEX_BIN" ]] \
     || [[ "$CODEX_BIN_SHA256" != "$RETHLAS_COHORT_CODEX_SHA256" ]]; }; then
  echo "Cohort Codex executable differs from its immutable intent." >&2
  exit 70
fi
codex_executable_unchanged() {
  local current
  current="$(attest_codex_binary "$CODEX_BIN")" || return 1
  RETHLAS_CODEX_ATTESTATION_JSON="$current" \
    "$TRUSTED_PYTHON_BIN" -I -B - \
      "$CODEX_BIN" "$CODEX_BIN_SHA256" <<'PY'
import json
import os
import sys

value = json.loads(os.environ["RETHLAS_CODEX_ATTESTATION_JSON"])
raise SystemExit(
    0
    if value == {"resolved_path": sys.argv[1], "sha256": sys.argv[2]}
    else 1
)
PY
}
CODEX_VERSION="$("$CODEX_BIN" --version 2>/dev/null || echo 'unknown')"

echo "========================================"
echo " Codex:      $CODEX_VERSION"
echo " Model:      $MODEL"
echo " Effort:     $REASONING_EFFORT"
echo " Main agent: $MAIN_AGENT_SELECTION"
echo " Mode:       core"
echo " Problem:    $PROBLEM_FILE"
echo " Problem ID: $problem_rel"
echo " References: $ref_dir"
echo " Math Python: $trusted_python_command"
echo " Max iters:  $MAX_ITERATIONS"
if [[ "$STOP_AFTER_CURRENT_COHORT" == 1 ]]; then
  echo " Cohort cap: stop after the current complete cohort"
else
  echo " Cohort cap: unrestricted inside each Legacy root"
fi
echo " Logs:       $LOG_DIR"
echo " Stop file:  $verified_path"
echo " Hot join:   physically unavailable"
echo " Cadence:    disabled"
echo "========================================"
echo ""

if [[ -n "${VERIFY_READY_URL:-}" ]]; then
  verify_base_url="${VERIFY_READY_URL%/ready}"
elif [[ -n "${VERIFY_HEALTH_URL:-}" ]]; then
  verify_base_url="${VERIFY_HEALTH_URL%/health}"
elif [[ -n "${VERIFY_URL:-}" ]]; then
  verify_base_url="${VERIFY_URL%/health}"
  verify_base_url="${verify_base_url%/ready}"
else
  verify_base_url="http://127.0.0.1:8091"
fi
VERIFY_READY_URL="${VERIFY_READY_URL:-${verify_base_url%/}/ready}"
VERIFY_HEALTH_URL="${VERIFY_HEALTH_URL:-${verify_base_url%/}/health}"
export VERIFY_PROOF_URL="${VERIFY_PROOF_URL:-${verify_base_url%/}/verify}"
if ! "$TRUSTED_PYTHON_BIN" -I -B - "$VERIFY_PROOF_URL" <<'PY'
import sys
from urllib.parse import urlsplit
url = urlsplit(sys.argv[1])
if url.scheme == "https":
    raise SystemExit(0)
if url.scheme == "http" and url.hostname in {"127.0.0.1", "localhost", "::1"}:
    raise SystemExit(0)
raise SystemExit(1)
PY
then
  echo "VERIFY_PROOF_URL must use HTTPS unless it targets loopback: $VERIFY_PROOF_URL" >&2
  exit 1
fi
verifier_is_ready() {
  curl -sf --connect-timeout 2 --max-time 5 \
    "$VERIFY_READY_URL" >/dev/null 2>&1
}
if ! verifier_is_ready; then
  if [[ "$ALLOW_OFFLINE_DRAFT" != 1 ]]; then
    echo "Verification service is not ready at ${VERIFY_READY_URL}." >&2
    echo "Refusing to start a paid Legacy root without its only completion authority." >&2
    echo "Start the verifier, or explicitly set RETHLAS_LEGACY_ALLOW_OFFLINE_DRAFT=1 for draft-only work." >&2
    exit 1
  fi
  echo "WARNING: explicit offline-draft mode is active; verified completion is unavailable."
  echo ""
fi
if verifier_is_ready; then
  verifier_profile_url="${VERIFY_READY_URL%/ready}/profile"
  if ! verifier_profile_json="$(
    curl -sf --connect-timeout 2 --max-time 5 "$verifier_profile_url"
  )"; then
    echo "Verifier profile endpoint is unavailable; refusing a paid Legacy root." >&2
    exit 1
  fi
  if ! RETHLAS_VERIFIER_PROFILE_JSON="$verifier_profile_json" \
       RETHLAS_EXPECTED_VERIFIER_PROFILE="$MODEL_POLICY_PROFILE" \
       "$TRUSTED_PYTHON_BIN" -I -B - <<'PY'
import json
import os

value = json.loads(os.environ["RETHLAS_VERIFIER_PROFILE_JSON"])
expected = os.environ["RETHLAS_EXPECTED_VERIFIER_PROFILE"]
passes = value.get("passes")
if (
    value.get("schema_version") != "rethlas_verifier_profile_v1"
    or value.get("profile") != expected
    or value.get("fallback_policy") != "forbid"
    or value.get("automatic_tiebreaker") is not False
    or not isinstance(passes, list)
    or len(passes) != 2
):
    raise SystemExit(1)
if any(
    not isinstance(item, dict)
    or item.get("model") == "gpt-5.6-sol"
    or item.get("launch_model") == "gpt-5.6-sol"
    for item in passes
):
    raise SystemExit(1)
if expected in {"balanced", "economy", "max_diversity"} and (
    passes[0].get("model") == passes[1].get("model")
):
    raise SystemExit(1)
if expected == "max_diversity" and not (
    passes[0].get("adapter") == "codex_cli"
    and passes[0].get("provider") == "openai"
    and passes[1].get("adapter") == "claude_cli"
    and passes[1].get("provider") not in {None, "openai"}
):
    raise SystemExit(1)
PY
  then
    echo "Verifier service does not match model-policy profile=$MODEL_POLICY_PROFILE; zero paid roots started." >&2
    exit 1
  fi
  unset verifier_profile_url verifier_profile_json
fi

if [[ -n "$EXTERNAL_PLAN_SET_SELECTION" ]]; then
  claude_core_fd_mode=0
  CLAUDE_CORE_SOURCE_FD=""
  CLAUDE_CORE_SOURCE_ORIGIN=""
  CLAUDE_CORE_LOGICAL_ORIGIN="$(cd "$ROOT_DIR/.." && pwd -P)/claude_core.py"
  claude_core_descriptor_sha256() {
    "$TRUSTED_PYTHON_BIN" -I -S -B - "$1" <<'PY'
import hashlib
import os
import stat
import sys

descriptor = int(sys.argv[1])
before = os.fstat(descriptor)
if not stat.S_ISREG(before.st_mode) or before.st_size > 16_000_000:
    raise SystemExit(1)
offset = 0
digest = hashlib.sha256()
while offset < before.st_size:
    chunk = os.pread(descriptor, min(65_536, before.st_size - offset), offset)
    if not chunk:
        raise SystemExit(1)
    digest.update(chunk)
    offset += len(chunk)
after = os.fstat(descriptor)
identity = lambda value: (
    value.st_dev,
    value.st_ino,
    value.st_mode,
    value.st_nlink,
    value.st_size,
    value.st_mtime_ns,
    value.st_ctime_ns,
)
if offset != before.st_size or identity(after) != identity(before):
    raise SystemExit(1)
print(digest.hexdigest())
PY
  }
  if [[ -n "$COHORT_HOST_SOURCE_FD" \
     || -n "$COHORT_HOST_SOURCE_SNAPSHOT" \
     || -n "$COHORT_HOST_SOURCE_ORIGIN" \
     || -n "$COHORT_HOST_SOURCE_SHA256" ]]; then
    if [[ ! "$COHORT_HOST_SOURCE_FD" =~ ^[0-9]+$ \
       || "$COHORT_HOST_SOURCE_FD" -lt 3 \
       || "$COHORT_HOST_SOURCE_SNAPSHOT" != /* \
       || "$COHORT_HOST_SOURCE_ORIGIN" != "$CLAUDE_CORE_LOGICAL_ORIGIN" \
       || ! "$COHORT_HOST_SOURCE_SHA256" =~ ^[0-9a-f]{64}$ \
       || ! -r "$descriptor_root/$COHORT_HOST_SOURCE_FD" \
       || ! -f "$COHORT_HOST_SOURCE_SNAPSHOT" \
       || -L "$COHORT_HOST_SOURCE_SNAPSHOT" ]]; then
      echo "Bound cohort host-source descriptor is invalid." >&2
      exit 70
    fi
    CLAUDE_CORE_SOURCE="$descriptor_root/$COHORT_HOST_SOURCE_FD"
    CLAUDE_CORE_SOURCE_SHA256="$COHORT_HOST_SOURCE_SHA256"
    CLAUDE_CORE_SOURCE_FD="$COHORT_HOST_SOURCE_FD"
    CLAUDE_CORE_SOURCE_ORIGIN="$COHORT_HOST_SOURCE_ORIGIN"
    claude_core_fd_mode=1
  else
    if [[ ! -f "$CLAUDE_CORE_LOGICAL_ORIGIN" \
       || -L "$CLAUDE_CORE_LOGICAL_ORIGIN" ]]; then
      echo "Claude core host source must be a regular non-symlink file." >&2
      exit 70
    fi
    exec {CLAUDE_CORE_SOURCE_FD}<"$CLAUDE_CORE_LOGICAL_ORIGIN"
    CLAUDE_CORE_SOURCE="$descriptor_root/$CLAUDE_CORE_SOURCE_FD"
    CLAUDE_CORE_SOURCE_ORIGIN="$CLAUDE_CORE_LOGICAL_ORIGIN"
    CLAUDE_CORE_SOURCE_SHA256="$({
      claude_core_descriptor_sha256 "$CLAUDE_CORE_SOURCE_FD"
    })" || {
      echo "Could not bind the Claude core host-source descriptor." >&2
      exit 70
    }
    claude_core_fd_mode=1
  fi
  claude_core_source_unchanged() {
    local current
    current="$({
      claude_core_descriptor_sha256 "$CLAUDE_CORE_SOURCE_FD"
    })" || return 1
    [[ "$current" == "$CLAUDE_CORE_SOURCE_SHA256" ]]
  }
  if ! claude_core_source_unchanged; then
    echo "Bound cohort host source differs from its immutable intent." >&2
    exit 70
  fi
  CLAUDE_CORE_FD_LOADER="$(cat <<'PY'
import hashlib
import os
import sys

descriptor = int(sys.argv[1])
expected = sys.argv[2]
origin = sys.argv[3]
metadata = os.fstat(descriptor)
if metadata.st_size > 16_000_000:
    raise SystemExit("authenticated cohort host source exceeds its byte cap")
offset = 0
chunks = []
while offset < metadata.st_size:
    chunk = os.pread(descriptor, min(65536, metadata.st_size - offset), offset)
    if not chunk:
        break
    chunks.append(chunk)
    offset += len(chunk)
source = b"".join(chunks)
if offset != metadata.st_size or hashlib.sha256(source).hexdigest() != expected:
    raise SystemExit("authenticated cohort host source binding mismatch")
sys.argv = [origin, *sys.argv[4:]]
globals()["__file__"] = origin
globals()["_RETHLAS_LOADED_SOURCE_SHA256"] = expected
exec(compile(source, origin, "exec"), globals(), globals())
PY
)"
  run_claude_core_source() {
    if [[ "$claude_core_fd_mode" == 1 ]]; then
      "$TRUSTED_PYTHON_BIN" -I -B -c "$CLAUDE_CORE_FD_LOADER" \
        "$CLAUDE_CORE_SOURCE_FD" "$CLAUDE_CORE_SOURCE_SHA256" \
        "$CLAUDE_CORE_SOURCE_ORIGIN" "$@"
    else
      "$TRUSTED_PYTHON_BIN" -I -B "$CLAUDE_CORE_SOURCE" "$@"
    fi
  }
  if ! EXTERNAL_PLAN_ACCEPTANCE_JSON="$({
    run_claude_core_source \
      --validate-plan-file "$EXTERNAL_PLAN_SET_SELECTION" \
      "$problem_rel" "$RETHLAS_EXPECTED_STATEMENT_SHA256" \
      "$EXTERNAL_PLAN_SHA256"
  })"; then
    echo "External Claude plan set failed closed; no Astra cohort was started." >&2
    exit 70
  fi
  if ! external_metadata="$({
    RETHLAS_EXTERNAL_PLAN_ACCEPTANCE_JSON="$EXTERNAL_PLAN_ACCEPTANCE_JSON" \
      "$TRUSTED_PYTHON_BIN" -I -B -c \
        'import json,os; value=json.loads(os.environ["RETHLAS_EXTERNAL_PLAN_ACCEPTANCE_JSON"]); print("\t".join(str(value[key]) for key in ("plan_path","root_session_id","plan_sha256","retrieval_mode")))'
  })"; then
    echo "Could not read validated Claude plan metadata." >&2
    exit 70
  fi
  IFS=$'\t' read -r EXTERNAL_PLAN_PATH EXTERNAL_PLAN_ROOT_SESSION_ID \
    accepted_external_plan_sha256 EXTERNAL_RETRIEVAL_MODE <<< "$external_metadata"
  if [[ "$accepted_external_plan_sha256" != "$EXTERNAL_PLAN_SHA256" ]]; then
    echo "Validated Claude plan digest changed." >&2
    exit 70
  fi
  case "$EXTERNAL_RETRIEVAL_MODE" in
    disabled|matlas_arxiv) ;;
    *) echo "Validated Claude plan has an unsupported retrieval mode." >&2; exit 70 ;;
  esac
  EXTERNAL_PLAN_RELATIVE="${EXTERNAL_PLAN_PATH#"$ROOT_DIR"/}"
  if [[ "$EXTERNAL_PLAN_RELATIVE" == "$EXTERNAL_PLAN_PATH" ]]; then
    echo "Claude plan input is not inside the generation workspace." >&2
    exit 70
  fi
  external_plan_unchanged() {
    claude_core_source_unchanged || return 1
    run_claude_core_source \
      --validate-plan-file "$EXTERNAL_PLAN_PATH" \
      "$problem_rel" "$RETHLAS_EXPECTED_STATEMENT_SHA256" \
      "$EXTERNAL_PLAN_SHA256" >/dev/null
  }
  unset EXTERNAL_PLAN_ACCEPTANCE_JSON
  EXTERNAL_PLAN_USED=1
  export RETHLAS_BOUND_EXTERNAL_PLAN_PATH="$EXTERNAL_PLAN_PATH"
  export RETHLAS_BOUND_EXTERNAL_PLAN_SHA256="$EXTERNAL_PLAN_SHA256"
  export RETHLAS_BOUND_EXTERNAL_PLAN_ROOT_SESSION_ID="${EXTERNAL_PLAN_ROOT_SESSION_ID}"

  # A SHA-bound plan is not a context boundary by itself.  The cohort executor
  # must not be able to recover another problem's statement, plans, memory, or
  # transcript by searching the shared workspace (or the user's CLI history).
  # Fail before model launch unless an unprivileged mount/PID namespace is
  # available, then materialize a per-problem view immediately around Codex.
  cohort_platform="$($TRUSTED_PYTHON_BIN -I -S -B -c 'import sys; print(sys.platform)')"
  if [[ "$cohort_platform" == linux ]]; then
  COHORT_ISOLATION_BACKEND="linux-mount-namespace"
  for isolation_tool in \
    /usr/bin/unshare /usr/bin/mount /usr/bin/umount /usr/bin/mktemp \
    /usr/bin/mkdir /usr/bin/touch /usr/bin/rmdir /usr/bin/cp \
    /usr/bin/chmod /bin/bash /bin/true; do
    if [[ ! -x "$isolation_tool" ]]; then
      echo "External Claude cohorts require filesystem isolation tool: $isolation_tool" >&2
      exit 70
    fi
  done
  if ! /usr/bin/unshare --user --map-root-user --mount --pid --fork \
      --mount-proc /bin/true 2>/dev/null; then
    echo "External Claude cohorts require an available unprivileged mount/PID namespace; zero paid executors started." >&2
    exit 70
  fi
  if ! "$TRUSTED_PYTHON_BIN" -I -B - "$ROOT_DIR" "$problem_rel" <<'PY'
import os
import stat
import sys
from pathlib import Path

root = Path(sys.argv[1]).resolve(strict=True)
parts = tuple(Path(sys.argv[2]).parts)
if not parts or any(part in {"", ".", ".."} for part in parts):
    raise SystemExit("invalid cohort problem id")
for category in ("memory", "results"):
    cursor = root / category
    metadata = cursor.lstat()
    if cursor.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
        raise SystemExit(f"unsafe cohort {category} root")
    for part in parts:
        cursor /= part
        try:
            metadata = cursor.lstat()
        except FileNotFoundError:
            cursor.mkdir(mode=0o700)
            metadata = cursor.lstat()
        if cursor.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
            raise SystemExit(f"unsafe cohort {category} path")
        if not cursor.resolve(strict=True).is_relative_to(root / category):
            raise SystemExit(f"cohort {category} path escapes its root")
PY
  then
    echo "Could not prepare isolated current-problem memory/results directories." >&2
    exit 70
  fi
  COHORT_NAMESPACE_PROGRAM="$(cat <<'BASH'
set -euo pipefail

root="$1"
problem_file="$2"
problem_id="$3"
plan_relative="$4"
reference_relative="$5"
candidate_relative="$6"
home_dir="$7"
temporary_root="$8"
codex_bin="$9"
shift 9

/usr/bin/mount --make-rprivate /
if [[ -L /root || ! -d /root ]]; then
  echo "cohort isolation requires an ordinary /root mountpoint" >&2
  exit 70
fi
# Bubblewrap hands the command shell socket-backed standard streams.  Bash can
# classify that non-login invocation as a remote shell and consult the UID 0
# passwd home independently of HOME and BASH_ENV.  Hide the host root home and
# give that lookup an empty, private mount so startup files cannot be read and
# their permission errors cannot contaminate structured tool output.
/usr/bin/mount -t tmpfs -o mode=0700,nosuid,nodev,size=1m \
  rethlas-empty-root-home /root
stage="$(/usr/bin/mktemp -d "$temporary_root/rethlas-cohort-capsule.XXXXXXXX")"
root_source="$stage/generation"
codex_source="$stage/codex-home"
/usr/bin/mkdir "$root_source"
root_source_mounted=0
codex_source_mounted=0

cleanup_stage() {
  set +e
  if [[ "$codex_source_mounted" == 1 ]]; then
    /usr/bin/umount "$codex_source" >/dev/null 2>&1
  fi
  if [[ "$root_source_mounted" == 1 ]]; then
    /usr/bin/umount "$root_source" >/dev/null 2>&1
  fi
  /usr/bin/rmdir "$codex_source" "$root_source" "$stage" >/dev/null 2>&1
}
trap cleanup_stage EXIT

bind_read_only_directory() {
  local source="$1"
  local target="$2"
  /usr/bin/mkdir -p "$target"
  /usr/bin/mount --bind "$source" "$target"
  /usr/bin/mount -o remount,bind,ro,nosuid,nodev "$target"
}

bind_read_only_file() {
  local source="$1"
  local target="$2"
  /usr/bin/mkdir -p "${target%/*}"
  /usr/bin/touch "$target"
  /usr/bin/mount --bind "$source" "$target"
  /usr/bin/mount -o remount,bind,ro,nosuid,nodev "$target"
}

copy_private_runtime_file() {
  local source="$1"
  local target="$2"
  /usr/bin/mkdir -p "${target%/*}"
  /usr/bin/cp -- "$source" "$target"
  /usr/bin/chmod 0600 "$target"
}

/usr/bin/mount --bind "$root" "$root_source"
root_source_mounted=1
/usr/bin/mount -t tmpfs -o mode=0700,nosuid,nodev,size=512m \
  rethlas-cohort-generation "$root"

bind_read_only_directory "$root_source/.agents" "$root/.agents"
bind_read_only_directory "$root_source/.codex" "$root/.codex"
bind_read_only_directory "$root_source/mcp" "$root/mcp"
bind_read_only_file "$root_source/AGENTS.legacy.md" "$root/AGENTS.legacy.md"
bind_read_only_file "$root_source/$problem_file" "$root/$problem_file"
bind_read_only_file "$root_source/$plan_relative" "$root/$plan_relative"
if [[ -d "$root_source/$reference_relative" \
   && ! -L "$root_source/$reference_relative" ]]; then
  bind_read_only_directory \
    "$root_source/$reference_relative" "$root/$reference_relative"
fi
if [[ -d "$root_source/$candidate_relative" \
   && ! -L "$root_source/$candidate_relative" ]]; then
  bind_read_only_directory \
    "$root_source/$candidate_relative" "$root/$candidate_relative"
fi

/usr/bin/mkdir -p "$root/memory/$problem_id" "$root/results/$problem_id"
/usr/bin/mount --bind \
  "$root_source/memory/$problem_id" "$root/memory/$problem_id"
/usr/bin/mount --bind \
  "$root_source/results/$problem_id" "$root/results/$problem_id"

# Remove other host-side AxiomRelay state and repository history from every path
# visible in this mount namespace.  The detached owner worker remains outside
# the namespace and retains its durable state and log descriptors.
agents_root="${root%/*}"
repository_root="${agents_root%/*}"
if [[ -L "$agents_root/.claude_core" ]]; then
  echo "cohort isolation refused a symlinked Claude state root" >&2
  exit 70
elif [[ -d "$agents_root/.claude_core" ]]; then
  /usr/bin/mount -t tmpfs -o mode=0700,nosuid,nodev,size=16m \
    rethlas-empty-claude-core "$agents_root/.claude_core"
fi
if [[ -L "$repository_root/.git" ]]; then
  echo "cohort isolation refused a symlinked Git control path" >&2
  exit 70
elif [[ -d "$repository_root/.git" ]]; then
  /usr/bin/mount -t tmpfs -o mode=0700,nosuid,nodev,size=4m \
    rethlas-empty-git "$repository_root/.git"
elif [[ -f "$repository_root/.git" ]]; then
  /usr/bin/mount --bind /dev/null "$repository_root/.git"
  /usr/bin/mount -o remount,bind,ro,nosuid,nodev "$repository_root/.git"
fi

# Codex needs its installed binary and authentication material, but not prior
# sessions, memories, logs, queues, plugins, or user configuration.  Rebuild a
# minimal ephemeral CODEX_HOME view when the ordinary home exists.  The
# app-server may refresh auth.json and always creates its own installation id,
# model cache, and SQLite state.  Give it a private writable copy of auth.json
# and let every other runtime file be born inside the tmpfs; read-only bind
# mounts of those mutable files fail during app-server initialization.
if [[ "$home_dir" == /* && -L "$home_dir/.codex" ]]; then
  echo "cohort isolation refused a symlinked Codex home" >&2
  exit 70
elif [[ "$home_dir" == /* && -d "$home_dir/.codex" ]]; then
  /usr/bin/mkdir "$codex_source"
  /usr/bin/mount --bind "$home_dir/.codex" "$codex_source"
  codex_source_mounted=1
  /usr/bin/mount -t tmpfs -o mode=0700,nosuid,nodev,size=128m \
    rethlas-ephemeral-codex-home "$home_dir/.codex"
  if [[ -f "$codex_source/auth.json" \
     && ! -L "$codex_source/auth.json" ]]; then
    copy_private_runtime_file \
      "$codex_source/auth.json" "$home_dir/.codex/auth.json"
  fi
  if [[ -d "$codex_source/packages" && ! -L "$codex_source/packages" ]]; then
    bind_read_only_directory \
      "$codex_source/packages" "$home_dir/.codex/packages"
  fi
fi
if [[ "$home_dir" == /* && -L "$home_dir/.claude" ]]; then
  echo "cohort isolation refused a symlinked Claude home" >&2
  exit 70
elif [[ "$home_dir" == /* && -d "$home_dir/.claude" ]]; then
  /usr/bin/mount -t tmpfs -o mode=0700,nosuid,nodev,size=16m \
    rethlas-empty-claude-home "$home_dir/.claude"
fi

/usr/bin/umount "$root_source"
root_source_mounted=0
if [[ "$codex_source_mounted" == 1 ]]; then
  /usr/bin/umount "$codex_source"
  codex_source_mounted=0
fi
/usr/bin/rmdir "$codex_source" "$root_source" "$stage" 2>/dev/null || true
trap - EXIT

cd "$root"
exec "$codex_bin" "$@"
BASH
)"
  elif [[ "$cohort_platform" == darwin ]]; then
    COHORT_ISOLATION_BACKEND="macos-codex-seatbelt"
    if ! "$TRUSTED_PYTHON_BIN" -I -S -B - \
        "$ROOT_DIR" "$problem_rel" <<'PY'
import os
import stat
import sys
from pathlib import Path

root = Path(sys.argv[1]).resolve(strict=True)
parts = tuple(Path(sys.argv[2]).parts)
if not parts or any(part in {"", ".", ".."} for part in parts):
    raise SystemExit("invalid cohort problem id")
allowed_uids = {0, os.geteuid()}
directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
directory_flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
root_descriptor = os.open(root, directory_flags)
try:
    for category in ("memory", "results"):
        try:
            os.mkdir(category, 0o700, dir_fd=root_descriptor)
        except FileExistsError:
            pass
        descriptor = os.open(category, directory_flags, dir_fd=root_descriptor)
        try:
            metadata = os.fstat(descriptor)
            observed = os.stat(
                category, dir_fd=root_descriptor, follow_symlinks=False
            )
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or not stat.S_ISDIR(observed.st_mode)
                or metadata.st_uid not in allowed_uids
                or stat.S_IMODE(metadata.st_mode) & 0o022
                or (metadata.st_dev, metadata.st_ino)
                != (observed.st_dev, observed.st_ino)
            ):
                raise SystemExit(f"unsafe cohort {category} root")
            for part in parts:
                try:
                    os.mkdir(part, 0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
                child = os.open(part, directory_flags, dir_fd=descriptor)
                metadata = os.fstat(child)
                observed = os.stat(part, dir_fd=descriptor, follow_symlinks=False)
                if (
                    not stat.S_ISDIR(metadata.st_mode)
                    or not stat.S_ISDIR(observed.st_mode)
                    or metadata.st_uid not in allowed_uids
                    or stat.S_IMODE(metadata.st_mode) & 0o022
                    or (metadata.st_dev, metadata.st_ino)
                    != (observed.st_dev, observed.st_ino)
                ):
                    os.close(child)
                    raise SystemExit(f"unsafe cohort {category} path")
                os.close(descriptor)
                descriptor = child
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    os.fsync(root_descriptor)
finally:
    os.close(root_descriptor)
PY
    then
      echo "Could not prepare isolated current-problem memory/results directories." >&2
      exit 70
    fi
    codex_profile_origin="${RETHLAS_COHORT_CODEX_BIN:-$CODEX_BIN}"
    if [[ "$codex_profile_origin" != /* ]]; then
      echo "macOS cohort isolation requires an absolute Codex executable origin." >&2
      exit 70
    fi
    COHORT_PERMISSION_FILESYSTEM_TOML="$({
      "$TRUSTED_PYTHON_BIN" -I -S -B - \
        "$ROOT_DIR" "$codex_profile_origin" "$CODEX_BIN" \
        "$PROBLEM_FILE" "$EXTERNAL_PLAN_RELATIVE" "$ref_dir" \
        "$candidate_projection_dir" "$problem_rel" <<'PY'
import json
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
codex_paths = [pathlib.Path(sys.argv[2]), pathlib.Path(sys.argv[3])]
problem_file = sys.argv[4]
plan_relative = sys.argv[5]
reference_relative = sys.argv[6]
candidate_relative = sys.argv[7]
problem_id = pathlib.Path(sys.argv[8])

entries = {":minimal": "read", str(root): "deny"}
for path in codex_paths:
    if path.is_absolute():
        entries[str(path)] = "read"
for relative in (".agents", ".codex", "mcp", "AGENTS.legacy.md"):
    path = root / relative
    if path.exists() and not path.is_symlink():
        entries[str(path)] = "read"
for relative in (problem_file, plan_relative):
    path = root / relative
    if not path.is_file() or path.is_symlink():
        raise SystemExit(f"unsafe cohort input: {path}")
    entries[str(path)] = "read"
for relative in (reference_relative, candidate_relative):
    path = root / relative
    if path.is_dir() and not path.is_symlink():
        entries[str(path)] = "read"
for category in ("memory", "results"):
    path = root / category / problem_id
    if not path.is_dir() or path.is_symlink():
        raise SystemExit(f"unsafe cohort output: {path}")
    entries[str(path)] = "write"

print(
    "{" + ",".join(
        json.dumps(path) + "=" + json.dumps(mode)
        for path, mode in entries.items()
    ) + "}"
)
PY
    })" || {
      echo "Could not construct the macOS cohort permission profile." >&2
      exit 70
    }
    COHORT_PERMISSION_CONFIG_ARGS=(
      --config 'default_permissions="axiom-relay-cohort"'
      --config "permissions.axiom-relay-cohort.filesystem=$COHORT_PERMISSION_FILESYSTEM_TOML"
      --config 'permissions.axiom-relay-cohort.network.enabled=false'
      --config 'approval_policy="never"'
    )
    cohort_probe_path="$ROOT_DIR/results/$problem_rel/.axiom-isolation-probe"
    if ! "$CODEX_BIN" sandbox \
      --permission-profile axiom-relay-cohort \
      --config "permissions.axiom-relay-cohort.filesystem=$COHORT_PERMISSION_FILESYSTEM_TOML" \
      --config 'permissions.axiom-relay-cohort.network.enabled=false' \
      -C "$ROOT_DIR" -- /bin/sh -c \
      'test -r "$1" && test ! -r "$2" && : > "$3"' \
      axiom-relay-cohort \
      "$ROOT_DIR/$PROBLEM_FILE" "$ROOT_DIR/tests/run_legacy.sh" \
      "$cohort_probe_path"; then
      echo "macOS Codex Seatbelt cohort isolation probe failed; zero paid executors started." >&2
      exit 70
    fi
    if ! "$TRUSTED_PYTHON_BIN" -I -S -B - "$cohort_probe_path" <<'PY'
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
if not path.is_file() or path.is_symlink():
    raise SystemExit(1)
path.unlink()
PY
    then
      echo "macOS cohort isolation probe cleanup failed." >&2
      exit 70
    fi
    unset cohort_probe_path codex_profile_origin
  else
    echo "External Claude cohorts support only Linux and macOS." >&2
    exit 70
  fi
  echo "Accepted Claude root plan set: $EXTERNAL_PLAN_SHA256"
  echo "Claude root session: $EXTERNAL_PLAN_ROOT_SESSION_ID"
  echo "Claude cohort retrieval mode: $EXTERNAL_RETRIEVAL_MODE"
  echo ""
fi

export RETHLAS_RUNTIME_PROFILE="legacy"
TRUSTED_MCP_ENV_TOML="$("$TRUSTED_PYTHON_BIN" -I -B - <<'PY'
import json
import os

names = (
    "PYTHONDONTWRITEBYTECODE",
    "RETHLAS_EXPECTED_PROBLEM_ID",
    "RETHLAS_EXPECTED_STATEMENT_SHA256",
    "RETHLAS_BOUND_EXTERNAL_PLAN_PATH",
    "RETHLAS_BOUND_EXTERNAL_PLAN_SHA256",
    "RETHLAS_BOUND_EXTERNAL_PLAN_ROOT_SESSION_ID",
    "RETHLAS_GENERATION_ROOT",
    "RETHLAS_RECEIPTS_ROOT",
    "RETHLAS_RUNTIME_PROFILE",
    "RETHLAS_MODEL_POLICY_PROFILE",
    "VERIFY_API_TOKEN",
    "VERIFY_PROOF_URL",
)
entries = [
    f"{json.dumps(name)} = {json.dumps(os.environ[name])}"
    for name in names
    if name in os.environ
]
print("{" + ", ".join(entries) + "}")
PY
)"
TRUSTED_REASONING_MCP_BASE_TOML="{tool_timeout_sec=3600,command=$TRUSTED_PYTHON_COMMAND_TOML,args=$TRUSTED_MCP_ARGS_TOML,cwd=$TRUSTED_MCP_CWD_TOML,env=$TRUSTED_MCP_ENV_TOML,required=true,default_tools_approval_mode=\"approve\"}"
TRUSTED_REASONING_AGENT_MCP_TOML="${TRUSTED_REASONING_MCP_BASE_TOML%?},disabled_tools=[\"memory_append_batch\"]}"
TRUSTED_REASONING_CHECKPOINT_BASE_TOML="${TRUSTED_REASONING_MCP_BASE_TOML/tool_timeout_sec=3600/tool_timeout_sec=60}"
TRUSTED_REASONING_CHECKPOINT_PRIMARY_MCP_TOML="${TRUSTED_REASONING_CHECKPOINT_BASE_TOML%?},enabled_tools=[\"memory_append_batch\"]}"
TRUSTED_REASONING_CHECKPOINT_RECOVERY_MCP_TOML="${TRUSTED_REASONING_CHECKPOINT_BASE_TOML%?},enabled_tools=[\"memory_append_batch\"]}"

START_EPOCH=$(date +%s)

elapsed_timer() {
  local timer_sleep_pid=""
  trap '
    if [[ -n "${timer_sleep_pid:-}" ]]; then
      kill "$timer_sleep_pid" 2>/dev/null || true
      wait "$timer_sleep_pid" 2>/dev/null || true
    fi
    exit 0
  ' TERM INT HUP
  while true; do
    sleep "$TIMER_INTERVAL_SECONDS" &
    timer_sleep_pid=$!
    wait "$timer_sleep_pid" || exit 0
    timer_sleep_pid=""
    local now
    now=$(date +%s)
    local secs=$((now - START_EPOCH))
    printf "\r  [elapsed %s] still running..." "$(format_duration "$secs")"
  done
}

elapsed_timer &
TIMER_PID=$!

cleanup_timer() {
  kill "$TIMER_PID" 2>/dev/null || true
  wait "$TIMER_PID" 2>/dev/null || true
}
trap cleanup_timer EXIT

NO_PROGRESS_STOP=0
for ((iter = 0; iter < MAX_ITERATIONS; iter += 1)); do
  log_file="$LOG_DIR/${problem_name}_iter_${iter}.md"

  if ! trusted_runtime_unchanged; then
    echo "Trusted generation runtime changed; refusing to start another session." >&2
    exit 70
  fi
  if ! codex_executable_unchanged; then
    echo "Codex executable changed; refusing to start another legacy session." >&2
    exit 70
  fi
  if [[ "$EXTERNAL_PLAN_USED" == 1 ]] \
     && ! external_plan_unchanged; then
    echo "Claude root plan input changed; refusing another paid executor." >&2
    exit 70
  fi

  if receipt_is_valid; then
    echo "Solved problem_id=$problem_rel before iter=$iter"
    break
  fi

  offline_prompt=""
  if ! verifier_is_ready; then
    if [[ "$ALLOW_OFFLINE_DRAFT" != 1 ]]; then
      echo "Verification service became unavailable before iter=$iter; refusing another paid root." >&2
      exit 1
    fi
    offline_prompt="The owner explicitly enabled draft-only work while the verifier is unavailable. Do not repeatedly call the verifier or claim success; preserve only frontier-changing draft or checkpoint work."
  fi
  frontier_before_receipt="$(legacy_frontier_receipt)" || exit 70
  frontier_before="$({
    legacy_frontier_sha_from_receipt "$frontier_before_receipt"
  })" || exit 70

  echo "Starting iter=$iter -> $log_file"

  if [[ "$iter" -eq 0 ]]; then
    prompt="Follow the injected AGENTS.legacy.md developer profile exactly to solve the math problem in ${PROBLEM_FILE}. Use problem_id=${problem_rel}. ${ref_prompt} A trusted math-research runtime is available as both python and python3, with NumPy, SciPy, SymPy, mpmath, and gmpy2 importable. This is iteration 0 in a fresh session. Begin with the protected root route-design phase, with ${DEEP_WORK_MINUTES} minutes as a soft target that must not delay a ready fanout: after reading the authoritative problem and local references, do not initialize or write memory, retrieve externally, update branches, or spawn collaborators until either a complete candidate exists or exactly three materially different, scope-disjoint routes have been screened for duplication, obvious contradiction, and basic viability. If no candidate exists, invoke \$legacy-three-route for the one pre-fanout checkpoint and exact three context-free solvers. The root must not pursue a fourth proof route. Necessary local exact, symbolic, or numerical computation is allowed. Any complete candidate preempts the fanout or remaining waits and enters the candidate fast lane for verifier publication. Ignore any pre-existing blueprint_verified.md: only verify_blueprint_service and its trusted receipt can finish this run. On a truthful non-success, persist frontier-changing mathematical work and return unverified."
    web_mode="disabled"
  elif ((iter % 2 == 1)); then
    prompt="Start a fresh reasoning session under the injected AGENTS.legacy.md developer profile and continue problem_id=${problem_rel}. Read ${PROBLEM_FILE}, the current results/${problem_rel}/blueprint.md if it exists, and use at most one bounded memory_search only when essential state is missing. Continue the current three-route generation or, only after its three reports and shared failure synthesis are durable, design the next exact three-route fanout through \$legacy-three-route. The trusted local runtime still provides NumPy, SciPy, SymPy, mpmath, and gmpy2. Ignore any pre-existing blueprint_verified.md: only verify_blueprint_service and its trusted receipt can finish this run. This is iteration ${iter}. Do not use arXiv theorem search or web search. If a complete candidate appears, enter the candidate fast lane immediately."
    web_mode="disabled"
  else
    prompt="Start a fresh reasoning session under the injected AGENTS.legacy.md developer profile and continue problem_id=${problem_rel}. Read ${PROBLEM_FILE}, the current results/${problem_rel}/blueprint.md if it exists, and use at most one bounded memory_search only when essential state is missing. Continue the current three-route generation or, only after its three reports and shared failure synthesis are durable, design the next exact three-route fanout through \$legacy-three-route. The trusted local runtime still provides NumPy, SciPy, SymPy, mpmath, and gmpy2. Ignore any pre-existing blueprint_verified.md: only verify_blueprint_service and its trusted receipt can finish this run. This is iteration ${iter}. arXiv theorem search and web search are capabilities, not obligations: use them only for one named external knowledge gap under the two-query budget, then return to reasoning. If a complete candidate appears, enter the candidate fast lane immediately."
    web_mode="live"
  fi
  if [[ "$EXTERNAL_PLAN_USED" == 1 ]]; then
    if [[ "$EXTERNAL_RETRIEVAL_MODE" == matlas_arxiv ]]; then
      external_retrieval_prompt="The SHA-bound problem explicitly permits only the dedicated search_matlas_theorems, search_arxiv_theorems, and read_arxiv_primary tools. General web search, browser access, and every other remote source remain forbidden. Initial retrieval calls are zero. After the required pre-fanout checkpoint, a solver may use at most two targeted queries for one explicit named knowledge gap that can change its assigned route. Official arXiv metadata enforces the statement cutoff before search snippets enter context. A returned exact arXiv id may then be inspected at a bounded locator with read_arxiv_primary; that primary-source follow-up is not another search query and re-enforces the same cutoff. Propagate this exact retrieval mode and budget to all three context-free solvers. Persist terminal reports through append_route_terminal_report instead of hand-building their schema or SHA-256."
    else
      external_retrieval_prompt="The SHA-bound problem does not permit external retrieval. Do not call Matlas, arXiv, web, browser, or any other remote source, and propagate this disabled mode to all three context-free solvers."
    fi
    prompt="${prompt} This physical GPT Astra process is a bounded cohort executor for a persistent Claude canonical root, not a route-design root. Before any mathematical action, read ${EXTERNAL_PLAN_RELATIVE} and verify its SHA-256 is ${EXTERNAL_PLAN_SHA256}. The plan set is host-validated and bound to Claude root session ${EXTERNAL_PLAN_ROOT_SESSION_ID}, with statement_bound_retrieval_mode=${EXTERNAL_RETRIEVAL_MODE}. Skip fresh route generation and do not replace, rename, broaden, or add plans. Publish the ordinary one pre-fanout checkpoint by calling memory_append_batch exactly once with problem_id=${problem_rel} and items=[]; the trusted MCP atomically materializes and revalidates the exact three host-bound plans. Do not manually reproduce their JSON in that call. Then invoke \$legacy-three-route to launch exactly one context-free solver per plan. The host has placed this executor in a per-problem filesystem capsule: the visible statement, external plan, declared SHA-bound reference-candidate projection, memory, and results are the complete authorized local inputs. Read every candidate projection named by the bound plan before relying on or auditing it. Never inspect a parent directory, search for MCP/checkpoint examples, or look for any other plan, memory, log, result, statement, Git object, or CLI transcript; the tool schema and \$legacy-three-route payload rule are authoritative. Persist each exact non-candidate terminal report immediately, persist one shared failure synthesis after all three, and return unverified without another cohort. On a non-candidate round, do not create or rewrite results/${problem_rel}/blueprint.md: partial draft changes fall outside the exact permitted completion delta. Keep partial mathematics in the terminal reports or separately named notes, and propagate this restriction to all three children. A complete candidate may still write the working blueprint for verification. ${external_retrieval_prompt} Do not act as a fourth proof route. A complete candidate may still preempt into the existing verifier fast lane."
    web_mode="disabled"
  fi
  if [[ "$STOP_AFTER_CURRENT_COHORT" == 1 ]]; then
    prompt="${prompt} The owner selected the Legacy stop-after-current-cohort gate. Run at most one exact three-route cohort in this physical root. After its non-candidate reports have each been durably written ahead and the shared failure synthesis is durable, return unverified immediately. Do not checkpoint or spawn another cohort in this turn. A complete candidate still preempts into verification."
  fi
  prompt="${prompt} On non-success, do not call generation_yield. ${offline_prompt}"

  # Codex collaboration continuations can outlive the foreground `codex exec`
  # PID while retaining its stdout/stderr writer.  A direct file redirect would
  # let this shell inspect the frontier and emit a terminal receipt before those
  # continuations finish.  Drain through a pipe instead: the trusted copier
  # reaches EOF only after every inherited writer has closed.
  set +e
  (
    cd "$ROOT_DIR"
    codex_arguments=(exec \
      --strict-config \
      --ignore-user-config \
      --ignore-rules \
      --disable hooks \
      -C "$ROOT_DIR" \
      -m "$MODEL" \
      --config "model_reasoning_effort=\"$REASONING_EFFORT\"" \
      # Refuse an explicit login-shell request as a second guard around the
      # inert process- and tool-level Bash startup environment above.
      --config "allow_login_shell=false" \
      --config "agents.default_subagent_model=\"$MODEL\"" \
      --config "agents.default_subagent_reasoning_effort=\"$REASONING_EFFORT\"" \
      --config "project_doc_max_bytes=0" \
      --config "developer_instructions=$TRUSTED_LEGACY_INSTRUCTIONS_TOML" \
      --config "web_search=\"$web_mode\"" \
      --config "shell_environment_policy=$TRUSTED_SHELL_ENVIRONMENT_POLICY_TOML" \
      --config "mcp_servers.reasoning_agent=$TRUSTED_REASONING_AGENT_MCP_TOML" \
      --config "mcp_servers.reasoning_checkpoint_primary=$TRUSTED_REASONING_CHECKPOINT_PRIMARY_MCP_TOML" \
      --config "mcp_servers.reasoning_checkpoint_recovery=$TRUSTED_REASONING_CHECKPOINT_RECOVERY_MCP_TOML" \
      --ephemeral)
    if [[ "$EXTERNAL_PLAN_USED" == 1 ]]; then
      if [[ "$COHORT_ISOLATION_BACKEND" == linux-mount-namespace ]]; then
        codex_arguments+=(--sandbox workspace-write --skip-git-repo-check "$prompt")
        /usr/bin/unshare --user --map-root-user --mount --pid --fork \
          --mount-proc /bin/bash --noprofile --norc -c \
          "$COHORT_NAMESPACE_PROGRAM" rethlas-cohort-capsule \
          "$ROOT_DIR" "$PROBLEM_FILE" "$problem_rel" \
          "$EXTERNAL_PLAN_RELATIVE" "$ref_dir" "$candidate_projection_dir" \
          "${HOME:-}" \
          /tmp "$CODEX_BIN" "${codex_arguments[@]}"
      elif [[ "$COHORT_ISOLATION_BACKEND" == macos-codex-seatbelt ]]; then
        codex_arguments+=(
          "${COHORT_PERMISSION_CONFIG_ARGS[@]}"
          --skip-git-repo-check
          "$prompt"
        )
        "$CODEX_BIN" "${codex_arguments[@]}"
      else
        echo "External cohort isolation backend was not selected." >&2
        exit 70
      fi
    else
      codex_arguments+=(--sandbox workspace-write "$prompt")
      "$CODEX_BIN" "${codex_arguments[@]}"
    fi
  ) 2>&1 | "$TRUSTED_PYTHON_BIN" -I -B -c \
    'import sys
while block := sys.stdin.buffer.read1(65536):
    sys.stdout.buffer.write(block)
    sys.stdout.buffer.flush()' \
    >"$log_file"
  pipeline_status=("${PIPESTATUS[@]}")
  set -e
  codex_rc="${pipeline_status[0]}"
  log_drain_rc="${pipeline_status[1]}"

  if [[ "$log_drain_rc" -ne 0 ]]; then
    echo "Could not drain the complete Codex collaboration transcript at iter=$iter." >&2
    exit 70
  fi

  # ``codex exec`` writes its terminal token summary into the per-iteration
  # transcript, while the Claude cohort host can authenticate only this
  # runner's stdout/stderr capture.  Project the final numeric aggregate into
  # that capture so settlement telemetry does not incorrectly report
  # ``coverage=none``.  Native collaboration still does not expose a truthful
  # per-lane split, which remains explicitly unavailable.
  codex_token_usage="$({
    "$TRUSTED_PYTHON_BIN" -I -B - "$log_file" <<'PY'
import os
import re
import sys

path = sys.argv[1]
with open(path, "rb") as handle:
    size = handle.seek(0, os.SEEK_END)
    handle.seek(max(0, size - 65_536), os.SEEK_SET)
    tail = handle.read(65_536)
matches = re.findall(
    rb"tokens\s+used\s*\n?\s*([0-9][0-9,]*)", tail, re.IGNORECASE
)
if matches:
    normalized = matches[-1].replace(b",", b"")
    if normalized.isdigit() and int(normalized) > 0:
        print(int(normalized))
PY
  })" || {
    echo "Could not extract bounded Codex token telemetry at iter=$iter." >&2
    exit 70
  }
  if [[ -n "$codex_token_usage" ]]; then
    printf "Cohort aggregate tokens used\n%s\n" "$codex_token_usage"
  fi
  unset codex_token_usage

  if ! trusted_runtime_unchanged; then
    echo "Trusted generation runtime was modified during iter=$iter; refusing to continue or accept publication." >&2
    exit 70
  fi
  if ! codex_executable_unchanged; then
    echo "Codex executable changed during the legacy session." >&2
    exit 70
  fi
  if receipt_is_valid; then
    echo "Solved problem_id=$problem_rel at iter=$iter"
    break
  fi
  if [[ "$codex_rc" -ne 0 ]]; then
    echo "codex exited with code $codex_rc at iter=$iter (see $log_file for details)" >&2
    exit "$codex_rc"
  fi

  generation_receipt="$(generation_control_receipt)" || exit 70
  generation_state="$(
    generation_control_state_from_receipt "$generation_receipt"
  )" || exit 70
  if [[ "$generation_state" != running ]]; then
    echo "Isolated legacy generation cannot enter owner wait state=$generation_state." >&2
    exit 70
  fi

  frontier_after_receipt="$(legacy_frontier_receipt)" || exit 70
  frontier_after="$({
    legacy_frontier_sha_from_receipt "$frontier_after_receipt"
  })" || exit 70
  if [[ "$frontier_after" == "$frontier_before" ]]; then
    echo "No trusted Legacy frontier delta after iter=$iter; refusing another paid root." >&2
    echo "The untrusted terminal output remains available at $log_file for owner review." >&2
    NO_PROGRESS_STOP=1
    break
  fi

  echo "Finished problem_id=$problem_rel iter=$iter -> $log_file"
done

cleanup_timer
trap - EXIT

END_EPOCH=$(date +%s)
TOTAL=$((END_EPOCH - START_EPOCH))
printf "\n"

if receipt_is_valid; then
  echo "Solved problem_id=$problem_rel -> $verified_path"
  printf "Total time: %s\n" "$(format_duration "$TOTAL")"
  echo ""
  echo "To view results in the browser, run:"
  echo "  ./site/serve.sh"
  echo "Then open http://localhost:3264"
  exit 0
fi

if [[ "$NO_PROGRESS_STOP" == 1 ]]; then
  echo "Stopped without verified publication because the last paid root produced no trusted frontier progress." >&2
  printf "Total time: %s\n" "$(format_duration "$TOTAL")"
  exit 1
fi

echo "Reached MAX_ITERATIONS=$MAX_ITERATIONS without verified blueprint for problem_id=$problem_rel" >&2
printf "Total time: %s\n" "$(format_duration "$TOTAL")"
exit 1
