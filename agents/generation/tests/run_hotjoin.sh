#!/usr/bin/env bash
# Durable hot-join implementation. Invoke through run_example.sh.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROBLEM_FILE="${PROBLEM_FILE:-data/example.md}"
MODEL="${MODEL:-gpt-5.6-sol}"
REASONING_EFFORT="${REASONING_EFFORT:-max}"
MAX_ITERATIONS="${MAX_ITERATIONS:-10}"
DEEP_WORK_MINUTES="${RETHLAS_DEEP_WORK_MINUTES:-60}"
TIMER_INTERVAL_SECONDS="${TIMER_INTERVAL_SECONDS:-30}"
RETHLAS_HOTJOIN_RUN_ID="${RETHLAS_HOTJOIN_RUN_ID:-}"
RETHLAS_RUN_MODE="${RETHLAS_RUN_MODE:-}"
HOTJOIN_ADAPTER="$(cd "$ROOT_DIR/.." && pwd -P)/hotjoin_adapter.py"
HOTJOIN_DB_DEFAULT="$(cd "$ROOT_DIR/.." && pwd -P)/.rethlas_hotjoin/messages.sqlite3"
GUARDIAN_SOURCE="$(cd "$ROOT_DIR" && pwd -P)/guardian.py"
GUARDIAN_LAUNCHER="$(cd "$ROOT_DIR" && pwd -P)/guardian_launcher.py"
GUARDIAN_RUNNER="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/$(basename "${BASH_SOURCE[0]}")"
NONFRESH_RESUME_DRY_RUN="${RETHLAS_NONFRESH_RESUME_DRY_RUN:-0}"
NONFRESH_STALE_RECONCILE="${RETHLAS_NONFRESH_STALE_RECONCILE:-0}"
NONFRESH_RESUME_DB_COPY="${RETHLAS_NONFRESH_RESUME_DB_COPY:-}"
NONFRESH_EXPECTED_THREAD_ID="${RETHLAS_NONFRESH_EXPECTED_THREAD_ID:-}"
NONFRESH_EXPECTED_TURN_ID="${RETHLAS_NONFRESH_EXPECTED_TURN_ID:-}"
NONFRESH_CONTROL_ONLY=0
if [[ "$NONFRESH_RESUME_DRY_RUN" == 1 || "$NONFRESH_STALE_RECONCILE" == 1 ]]; then
  NONFRESH_CONTROL_ONLY=1
fi
HOTJOIN_DB="$HOTJOIN_DB_DEFAULT"
if [[ "$NONFRESH_RESUME_DRY_RUN" == 1 || "$NONFRESH_STALE_RECONCILE" == 1 ]]; then
  HOTJOIN_DB="$NONFRESH_RESUME_DB_COPY"
fi
ADVISOR_BRIDGE="$ROOT_DIR/../advisor_bridge.py"
ADVISOR_ROOT="$(cd "$ROOT_DIR/.." && pwd -P)/.rethlas_advisor"
ADVISOR_RECEIPTS_ROOT="$ADVISOR_ROOT/receipts"
REVIEW_CADENCE_POLICY="${RETHLAS_REVIEW_CADENCE_POLICY:-}"
CONTEXT_GUARD_POLICY="${RETHLAS_CONTEXT_GUARD_POLICY:-}"
GENERATION_PYTHON_SELECTION="${RETHLAS_GENERATION_PYTHON_BIN:-}"
unset RETHLAS_GENERATION_PYTHON_BIN

case "$RETHLAS_RUN_MODE" in
  reviewed)
    RETHLAS_RUN_MODE="hotjoin"
    ;;
  hotjoin|hot-join)
    RETHLAS_RUN_MODE="hotjoin"
    ;;
  *)
    echo "run_hotjoin.sh is the private reviewed runner; use run_example.sh to select a mode." >&2
    exit 1
    ;;
esac
PUBLIC_RUN_MODE="reviewed"

case "$NONFRESH_RESUME_DRY_RUN" in
  0|1) ;;
  *)
    echo "RETHLAS_NONFRESH_RESUME_DRY_RUN must be 0 or 1." >&2
    exit 1
    ;;
esac
case "$NONFRESH_STALE_RECONCILE" in
  0|1) ;;
  *)
    echo "RETHLAS_NONFRESH_STALE_RECONCILE must be 0 or 1." >&2
    exit 1
    ;;
esac
if [[ "$NONFRESH_RESUME_DRY_RUN" == 1 && "$NONFRESH_STALE_RECONCILE" == 1 ]]; then
  echo "Copied-ledger diagnosis and stale reconciliation are distinct one-shot modes." >&2
  exit 1
fi

if [[ -z "$RETHLAS_HOTJOIN_RUN_ID" ]]; then
  echo "RETHLAS_RUN_MODE=reviewed requires RETHLAS_HOTJOIN_RUN_ID." >&2
  exit 1
fi
if [[ ! "$RETHLAS_HOTJOIN_RUN_ID" =~ ^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$ ]]; then
  echo "RETHLAS_HOTJOIN_RUN_ID must match [A-Za-z0-9][A-Za-z0-9._:-]{0,127}." >&2
  exit 1
fi
if [[ "$NONFRESH_RESUME_DRY_RUN" == 1 || "$NONFRESH_STALE_RECONCILE" == 1 ]]; then
  if [[ -z "$RETHLAS_HOTJOIN_RUN_ID" ]]; then
    echo "Copied-ledger operation requires RETHLAS_HOTJOIN_RUN_ID." >&2
    exit 1
  fi
  if [[ -z "$NONFRESH_RESUME_DB_COPY" || "$NONFRESH_RESUME_DB_COPY" != /* ]]; then
    echo "Copied-ledger operation requires absolute RETHLAS_NONFRESH_RESUME_DB_COPY." >&2
    exit 1
  fi
  if [[ "$NONFRESH_STALE_RECONCILE" == 1 ]] \
    && [[ -z "$NONFRESH_EXPECTED_THREAD_ID" || -z "$NONFRESH_EXPECTED_TURN_ID" ]]; then
    echo "Stale reconciliation requires exact RETHLAS_NONFRESH_EXPECTED_THREAD_ID and RETHLAS_NONFRESH_EXPECTED_TURN_ID." >&2
    exit 1
  fi
elif [[ -n "$NONFRESH_RESUME_DB_COPY" ]]; then
  echo "RETHLAS_NONFRESH_RESUME_DB_COPY is accepted only in an explicit copied-ledger control mode." >&2
  exit 1
fi
if [[ "$NONFRESH_STALE_RECONCILE" != 1 ]] \
  && [[ -n "$NONFRESH_EXPECTED_THREAD_ID" || -n "$NONFRESH_EXPECTED_TURN_ID" ]]; then
  echo "Expected stale thread/turn ids are accepted only in explicit reconciliation mode." >&2
  exit 1
fi

# Legacy codex-exec runs remain available without claiming scheduler
# guarantees. Hot-join runs select the continuous supervisor and context guard
# by default. The bounded rethlas_route_review_150m_v2 policy remains an
# explicit compatibility selection. There is deliberately no free-form timing
# override: the reviewed offsets and context thresholds are committed by the
# adapter's immutable policy contract.
if [[ "$RETHLAS_RUN_MODE" == hotjoin ]]; then
  if [[ "$NONFRESH_CONTROL_ONLY" == 1 ]]; then
    REVIEW_CADENCE_POLICY="${REVIEW_CADENCE_POLICY:-rethlas_route_review_150m_v2}"
  else
    REVIEW_CADENCE_POLICY="${REVIEW_CADENCE_POLICY:-rethlas_continuous_supervisor_v1}"
  fi
  CONTEXT_GUARD_POLICY="${CONTEXT_GUARD_POLICY:-rethlas_context_guard_v1}"
else
  REVIEW_CADENCE_POLICY="${REVIEW_CADENCE_POLICY:-disabled}"
  CONTEXT_GUARD_POLICY="${CONTEXT_GUARD_POLICY:-disabled}"
fi

case "$REVIEW_CADENCE_POLICY" in
  disabled|rethlas_route_review_150m_v2|rethlas_continuous_supervisor_v1) ;;
  *)
    echo "Unsupported RETHLAS_REVIEW_CADENCE_POLICY: $REVIEW_CADENCE_POLICY" >&2
    exit 1
    ;;
esac
case "$CONTEXT_GUARD_POLICY" in
  disabled|rethlas_context_guard_v1) ;;
  *)
    echo "Unsupported RETHLAS_CONTEXT_GUARD_POLICY: $CONTEXT_GUARD_POLICY" >&2
    exit 1
    ;;
esac
if [[ "$REVIEW_CADENCE_POLICY" != disabled || "$CONTEXT_GUARD_POLICY" != disabled ]]; then
  if [[ -z "$RETHLAS_HOTJOIN_RUN_ID" ]]; then
    echo "Durable review cadence/context guard require RETHLAS_HOTJOIN_RUN_ID; refusing to start Codex." >&2
    exit 1
  fi
  if [[ "$REVIEW_CADENCE_POLICY" == disabled \
     || "$CONTEXT_GUARD_POLICY" != rethlas_context_guard_v1 ]]; then
    echo "Durable review cadence and context guard must be enabled as the fixed policy pair." >&2
    exit 1
  fi
fi

RESOLVED_COST_GATE_POLICY="${RETHLAS_COST_GATE_POLICY:-owner_gated}"
case "$RESOLVED_COST_GATE_POLICY" in
  owner_gated|disabled_by_owner) ;;
  *)
    echo "RETHLAS_COST_GATE_POLICY must be owner_gated or disabled_by_owner." >&2
    exit 1
    ;;
esac

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
else
  python_command="$(command -v python3 || true)"
fi
if [[ "$python_command" != /* ]] || [[ ! -x "$python_command" ]]; then
  echo "Generation python3 must resolve to an absolute executable path." >&2
  exit 1
fi
if ! command -v realpath >/dev/null 2>&1; then
  echo "realpath is required to validate the generation Python environment." >&2
  exit 1
fi
TRUSTED_PYTHON_BIN="$(cd "$(dirname "$python_command")" && pwd -P)/$(basename "$python_command")"
RETHLAS_RESOLVED_COST_POLICY_JSON="$("$TRUSTED_PYTHON_BIN" -I -B - \
  "$RESOLVED_COST_GATE_POLICY" <<'PY'
import json
import sys

print(json.dumps(
    {
        "schema_version": "rethlas_resolved_cost_policy_v1",
        "policy": sys.argv[1],
        "authority": "owner_wrapper",
    },
    allow_nan=False,
    ensure_ascii=False,
    sort_keys=True,
    separators=(",", ":"),
))
PY
)"
RETHLAS_RESOLVED_COST_POLICY_SHA256="$("$TRUSTED_PYTHON_BIN" -I -B -c \
  'import hashlib,sys; print(hashlib.sha256(sys.argv[1].encode("utf-8")).hexdigest())' \
  "$RETHLAS_RESOLVED_COST_POLICY_JSON")"
export RETHLAS_RESOLVED_COST_POLICY_JSON
export RETHLAS_RESOLVED_COST_POLICY_SHA256
unset RETHLAS_COST_GATE_POLICY
python_target="$(realpath "$TRUSTED_PYTHON_BIN")"
temporary_root="$(cd "${TMPDIR:-/tmp}" && pwd -P)"
for candidate in "$TRUSTED_PYTHON_BIN" "$python_target"; do
  if [[ "$candidate" == "$ROOT_DIR" || "$candidate" == "$ROOT_DIR"/* \
     || "$candidate" == "$temporary_root" || "$candidate" == "$temporary_root"/* ]]; then
    echo "Python environment must be outside the generation workspace and temporary directory: $candidate" >&2
    exit 1
  fi
done
if [[ "$TRUSTED_PYTHON_BIN" != "$python_target" ]]; then
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
TRUSTED_SHELL_ENVIRONMENT_POLICY_TOML="$(
  "$TRUSTED_PYTHON_BIN" -I -B -c \
    'import json, sys; print("{inherit=\"none\",set={PATH=" + json.dumps(sys.argv[1]) + "}}")' \
    "$SAFE_SHELL_PATH"
)"

NONFRESH_SOURCE_DB_SHA256_BEFORE=""
NONFRESH_COPY_DB_SHA256_BEFORE=""
NONFRESH_COPY_DB_DEVICE=""
NONFRESH_COPY_DB_INODE=""
NONFRESH_DB_OWNER_UID=""
NONFRESH_SOURCE_WAL_SIZE=""
NONFRESH_SOURCE_SHM_SIZE=""
NONFRESH_SOURCE_WAL_MANIFEST=""
NONFRESH_SOURCE_SHM_MANIFEST=""
NONFRESH_SOURCE_DB_DEVICE=""
NONFRESH_SOURCE_DB_INODE=""
NONFRESH_SOURCE_PREIMAGE_MANIFEST=""
NONFRESH_SOURCE_PREIMAGE_MANIFEST_SHA256=""
if [[ "$NONFRESH_RESUME_DRY_RUN" == 1 || "$NONFRESH_STALE_RECONCILE" == 1 ]]; then
  if ! nonfresh_db_digests="$(
	"$TRUSTED_PYTHON_BIN" -I -B - \
	  "$HOTJOIN_DB_DEFAULT" "$NONFRESH_RESUME_DB_COPY" <<'PY'
import hashlib
import json
import os
import stat
import sys
from pathlib import Path


def fail(message: str) -> None:
    print(f"invalid non-fresh resume DB copy: {message}", file=sys.stderr)
    raise SystemExit(70)


def inspect(path_text: str, label: str) -> tuple[Path, os.stat_result]:
    path = Path(path_text)
    if not path.is_absolute():
        fail(f"{label} path is not absolute")
    cursor = path
    while True:
        try:
            metadata = cursor.lstat()
        except OSError as exc:
            fail(f"cannot inspect {label} path component {cursor}: {exc}")
        if stat.S_ISLNK(metadata.st_mode):
            fail(f"{label} path traverses a symlink: {cursor}")
        if cursor.parent == cursor:
            break
        cursor = cursor.parent
    metadata = path.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or metadata.st_nlink != 1
        or metadata.st_size < 100
    ):
        fail(f"{label} must be one owner-owned regular non-hardlinked SQLite file")
    with path.open("rb") as handle:
        header = handle.read(16)
    if header != b"SQLite format 3\x00":
        fail(f"{label} is not a SQLite database")
    return path, metadata


def digest(path: Path, expected: os.stat_result) -> str:
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        opened = os.fstat(descriptor)
        identity = (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
        if identity != (
            expected.st_dev,
            expected.st_ino,
            expected.st_size,
            expected.st_mtime_ns,
        ):
            fail(f"database changed before secure read: {path}")
        value = hashlib.sha256()
        while chunk := os.read(descriptor, 1024 * 1024):
            value.update(chunk)
        after = os.fstat(descriptor)
        if identity != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            fail(f"database changed during secure read: {path}")
        return value.hexdigest()
    finally:
        os.close(descriptor)


def sidecar_manifest(path: Path, label: str) -> dict[str, object]:
    try:
        before = path.lstat()
    except FileNotFoundError:
        return {"present": False}
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
            fail(f"{label} sidecar changed before secure read: {path}")
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_uid != os.getuid()
            or opened.st_nlink != 1
            or opened.st_size > 64 * 1024 * 1024
            or stat.S_IMODE(opened.st_mode) & 0o077
        ):
            fail(f"{label} sidecar is not one owner-only regular file: {path}")
        digest_value = hashlib.sha256()
        remaining = opened.st_size
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                fail(f"{label} sidecar was truncated during secure read: {path}")
            digest_value.update(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            fail(f"{label} sidecar grew during secure read: {path}")
        after = os.fstat(descriptor)
        path_after = path.lstat()
        if identity != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ) or identity != (
            path_after.st_dev,
            path_after.st_ino,
            path_after.st_size,
            path_after.st_mtime_ns,
        ):
            fail(f"{label} sidecar changed during secure read: {path}")
        return {
            "present": True,
            "device": opened.st_dev,
            "inode": opened.st_ino,
            "owner_uid": opened.st_uid,
            "mode_octal": f"{stat.S_IMODE(opened.st_mode):04o}",
            "size": opened.st_size,
            "mtime_ns": opened.st_mtime_ns,
            "sha256": digest_value.hexdigest(),
        }
    finally:
        os.close(descriptor)


def manifest_json(value: dict[str, object]) -> str:
    return json.dumps(value, allow_nan=False, sort_keys=True, separators=(",", ":"))


source, source_stat = inspect(sys.argv[1], "source DB")
copy, copy_stat = inspect(sys.argv[2], "copied-ledger DB copy")
if (source_stat.st_dev, source_stat.st_ino) == (copy_stat.st_dev, copy_stat.st_ino):
    fail("copied-ledger DB copy aliases the source DB inode")
if stat.S_IMODE(source_stat.st_mode) != 0o600:
    fail("source DB must have exact owner-only mode 0600")
if stat.S_IMODE(copy_stat.st_mode) != 0o600:
    fail("copied-ledger DB copy must have exact owner-only mode 0600")
source_wal = Path(str(source) + "-wal")
source_wal_manifest = sidecar_manifest(source_wal, "source WAL")
source_wal_size = int(source_wal_manifest.get("size", 0))
if source_wal_size:
    fail(
        f"source DB has a non-empty SQLite WAL; create a transactionally "
        f"consistent owner copy first: {source_wal}"
    )
source_shm_manifest = sidecar_manifest(Path(str(source) + "-shm"), "source SHM")
source_shm_size = int(source_shm_manifest.get("size", 0))
for suffix in ("-wal", "-shm"):
    if os.path.lexists(str(copy) + suffix):
        fail(f"copied-ledger DB copy has a pre-existing SQLite sidecar: {copy}{suffix}")
source_sha256 = digest(source, source_stat)
copy_sha256 = digest(copy, copy_stat)
if source_sha256 != copy_sha256:
    fail("copied-ledger DB copy must byte-match the source DB before control")
source_after = source.lstat()
source_wal_after = sidecar_manifest(source_wal, "source WAL")
source_shm_after = sidecar_manifest(Path(str(source) + "-shm"), "source SHM")
if (
    (
        source_after.st_dev,
        source_after.st_ino,
        source_after.st_uid,
        stat.S_IMODE(source_after.st_mode),
        source_after.st_size,
        source_after.st_mtime_ns,
    )
    != (
        source_stat.st_dev,
        source_stat.st_ino,
        source_stat.st_uid,
        stat.S_IMODE(source_stat.st_mode),
        source_stat.st_size,
        source_stat.st_mtime_ns,
    )
    or source_wal_after != source_wal_manifest
    or source_shm_after != source_shm_manifest
):
    fail("source DB or sidecar changed across the outer preflight")
source_preimage_manifest = {
    "schema_version": "rethlas_recovery_source_preimage_v1",
    "database": {
        "device": source_stat.st_dev,
        "inode": source_stat.st_ino,
        "owner_uid": source_stat.st_uid,
        "mode_octal": f"{stat.S_IMODE(source_stat.st_mode):04o}",
        "size": source_stat.st_size,
        "mtime_ns": source_stat.st_mtime_ns,
        "sha256": source_sha256,
    },
    "wal": source_wal_manifest,
    "shm": source_shm_manifest,
}
source_preimage_manifest_json = manifest_json(source_preimage_manifest)
print(
    source_sha256,
    copy_sha256,
    copy_stat.st_dev,
    copy_stat.st_ino,
    copy_stat.st_uid,
    source_wal_size,
    source_shm_size,
    manifest_json(source_wal_manifest),
    manifest_json(source_shm_manifest),
    source_stat.st_dev,
    source_stat.st_ino,
    source_preimage_manifest_json,
    hashlib.sha256(source_preimage_manifest_json.encode("utf-8")).hexdigest(),
)
PY
  )"; then
    exit 70
  fi
  read -r NONFRESH_SOURCE_DB_SHA256_BEFORE NONFRESH_COPY_DB_SHA256_BEFORE \
    NONFRESH_COPY_DB_DEVICE NONFRESH_COPY_DB_INODE NONFRESH_DB_OWNER_UID \
    NONFRESH_SOURCE_WAL_SIZE NONFRESH_SOURCE_SHM_SIZE \
    NONFRESH_SOURCE_WAL_MANIFEST NONFRESH_SOURCE_SHM_MANIFEST \
    NONFRESH_SOURCE_DB_DEVICE NONFRESH_SOURCE_DB_INODE \
    NONFRESH_SOURCE_PREIMAGE_MANIFEST \
    NONFRESH_SOURCE_PREIMAGE_MANIFEST_SHA256 \
    <<<"$nonfresh_db_digests"
fi

# This is an owner-side control path for a legacy run, not a weakened generation
# path. It exits before statement preparation and runtime snapshots. The adapter
# is read and executed from the exact bytes attested below, and it receives only
# the disposable, byte-identical DB copy.
if [[ "$NONFRESH_CONTROL_ONLY" == 1 ]]; then
  if [[ ! -f "$HOTJOIN_ADAPTER" || -L "$HOTJOIN_ADAPTER" ]]; then
    echo "Hot-join adapter must be a regular non-symlink file." >&2
    exit 70
  fi
  NONFRESH_ADAPTER_SHA256="$(
    "$TRUSTED_PYTHON_BIN" -I -B - "$HOTJOIN_ADAPTER" <<'PY'
import hashlib
import os
import stat
import sys
from pathlib import Path

path = Path(os.path.abspath(sys.argv[1]))
before = path.lstat()
if (
    path.is_symlink()
    or not stat.S_ISREG(before.st_mode)
    or before.st_nlink != 1
    or before.st_uid not in {0, os.getuid()}
):
    raise SystemExit("unsafe hot-join adapter identity")
descriptor = os.open(
    path,
    os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
)
try:
    opened = os.fstat(descriptor)
    identity = (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
    if identity != (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns):
        raise SystemExit("hot-join adapter changed while opened")
    digest = hashlib.sha256()
    while chunk := os.read(descriptor, 65536):
        digest.update(chunk)
    after = os.fstat(descriptor)
    if identity != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
        raise SystemExit("hot-join adapter changed while hashed")
finally:
    os.close(descriptor)
print(digest.hexdigest())
PY
  )" || exit 70
  NONFRESH_ADAPTER_LOADER="$(cat <<'PY'
import hashlib, os, stat, sys
from pathlib import Path

path = Path(os.path.abspath(sys.argv[1]))
expected_sha256 = sys.argv[2]
before = path.lstat()
if (
    path.is_symlink()
    or not stat.S_ISREG(before.st_mode)
    or before.st_nlink != 1
    or before.st_uid not in {0, os.getuid()}
):
    raise SystemExit("secure copied-ledger adapter loader rejected source identity")
descriptor = os.open(
    path,
    os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
)
try:
    opened = os.fstat(descriptor)
    identity = (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
    if identity != (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns):
        raise SystemExit("secure copied-ledger adapter loader observed an identity race")
    chunks = []
    remaining = opened.st_size
    while remaining:
        chunk = os.read(descriptor, min(65536, remaining))
        if not chunk:
            raise SystemExit("secure copied-ledger adapter loader observed truncation")
        chunks.append(chunk)
        remaining -= len(chunk)
    if os.read(descriptor, 1):
        raise SystemExit("secure copied-ledger adapter loader observed growth")
    after = os.fstat(descriptor)
    if identity != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
        raise SystemExit("secure copied-ledger adapter loader observed a read race")
finally:
    os.close(descriptor)
source = b"".join(chunks)
if hashlib.sha256(source).hexdigest() != expected_sha256:
    raise SystemExit("secure copied-ledger adapter loader observed a SHA-256 mismatch")
sys.argv = [str(path), *sys.argv[3:]]
namespace = {
    "__builtins__": __builtins__,
    "__file__": str(path),
    "__name__": "__main__",
    "__package__": None,
}
exec(compile(source, str(path), "exec"), namespace, namespace)
PY
  )"
  run_nonfresh_adapter() {
    "$TRUSTED_PYTHON_BIN" -I -B -c "$NONFRESH_ADAPTER_LOADER" \
      "$HOTJOIN_ADAPTER" "$NONFRESH_ADAPTER_SHA256" "$@"
  }

  if ! policy_contract_json="$(run_nonfresh_adapter policy-contract)"; then
    echo "Could not read the guardian release policy; zero paid work was started." >&2
    exit 70
  fi
  if ! RETHLAS_POLICY_CONTRACT_JSON="$policy_contract_json" \
    "$TRUSTED_PYTHON_BIN" -I -B - <<'PY'
import hashlib
import json
import os
import re


def fail(message: str) -> None:
    raise SystemExit(f"invalid copied-ledger policy contract: {message}")


def canonical(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


try:
    value = json.loads(os.environ["RETHLAS_POLICY_CONTRACT_JSON"])
except (KeyError, json.JSONDecodeError, UnicodeError) as exc:
    fail(f"not strict JSON: {exc}")
if not isinstance(value, dict) or set(value) != {
    "schema_version",
    "review_cadence_policy",
    "continuous_supervisor_policy",
    "context_guard_policy",
    "contract_sha256",
}:
    fail("top-level fields mismatch")
if value["schema_version"] != "rethlas-policy-contract-v1":
    fail("schema mismatch")
review = value["review_cadence_policy"]
context = value["context_guard_policy"]
if not isinstance(review, dict) or review.get("policy_id") != "rethlas_route_review_150m_v2":
    fail("review policy identity mismatch")
if not isinstance(context, dict) or context.get("policy_id") != "rethlas_context_guard_v1":
    fail("context policy identity mismatch")
if type(review.get("guardian_enforcement_ready")) is not bool:
    fail("guardian_enforcement_ready must be an immutable boolean")
fixed_review = {
    "review_1_due_seconds": 3600,
    "review_1_deadline_seconds": 4200,
    "review_2_due_seconds": 7200,
    "review_2_deadline_seconds": 7800,
    "close_notice_due_seconds": 8820,
    "hard_stop_due_seconds": 9000,
    "cycle_seconds": 9000,
    "review_drain_grace_seconds": 300,
    "review_execution_grace_seconds": 300,
}
if any(review.get(name) != expected for name, expected in fixed_review.items()):
    fail("fixed 60/120/147/150-minute offsets mismatch")
if review.get("review_boundary_mode") != "cooperative_drain_then_deadline_interrupt":
    fail("review boundary mode mismatch")
if review.get("hard_stop_interrupt_is_expected") is not True:
    fail("hard-stop semantics mismatch")
for label, policy in (("review", review), ("context", context)):
    claimed = policy.get("policy_sha256")
    if not isinstance(claimed, str) or re.fullmatch(r"[0-9a-f]{64}", claimed) is None:
        fail(f"{label} policy SHA-256 is malformed")
    material = dict(policy)
    del material["policy_sha256"]
    if hashlib.sha256(canonical(material)).hexdigest() != claimed:
        fail(f"{label} policy SHA-256 mismatch")
claimed_contract = value.get("contract_sha256")
if not isinstance(claimed_contract, str) or re.fullmatch(
    r"[0-9a-f]{64}", claimed_contract
) is None:
    fail("contract SHA-256 is malformed")
material = dict(value)
del material["contract_sha256"]
if hashlib.sha256(canonical(material)).hexdigest() != claimed_contract:
    fail("contract SHA-256 mismatch")
PY
  then
    echo "Copied-ledger policy validation failed; zero paid work was started." >&2
    exit 70
  fi
  if [[ "$NONFRESH_RESUME_DRY_RUN" == 1 ]]; then
  if ! nonfresh_status_json="$(
    run_nonfresh_adapter --db "$HOTJOIN_DB" status \
      --run-id "$RETHLAS_HOTJOIN_RUN_ID"
  )"; then
    echo "Could not inspect the copied legacy run; zero paid work was started." >&2
    exit 70
  fi
  if ! nonfresh_cadence_json="$(
    run_nonfresh_adapter --db "$HOTJOIN_DB" cadence-control-state \
      --run-id "$RETHLAS_HOTJOIN_RUN_ID"
  )"; then
    echo "Could not project cadence on the copied legacy run; zero paid work was started." >&2
    exit 70
  fi
  if ! nonfresh_report="$(
    RETHLAS_NONFRESH_STATUS_JSON="$nonfresh_status_json" \
    RETHLAS_NONFRESH_CADENCE_JSON="$nonfresh_cadence_json" \
    RETHLAS_POLICY_CONTRACT_JSON="$policy_contract_json" \
      "$TRUSTED_PYTHON_BIN" -I -B - \
        "$HOTJOIN_DB_DEFAULT" "$HOTJOIN_DB" \
        "$NONFRESH_SOURCE_DB_SHA256_BEFORE" \
        "$NONFRESH_COPY_DB_SHA256_BEFORE" \
        "$RETHLAS_HOTJOIN_RUN_ID" "$HOTJOIN_ADAPTER" \
        "$NONFRESH_ADAPTER_SHA256" "$NONFRESH_SOURCE_DB_DEVICE" \
        "$NONFRESH_SOURCE_DB_INODE" "$NONFRESH_COPY_DB_DEVICE" \
        "$NONFRESH_COPY_DB_INODE" <<'PY'
import hashlib
import json
import os
import re
import stat
import sys
from pathlib import Path


def fail(message: str) -> None:
    print(f"invalid non-fresh resume dry-run observation: {message}", file=sys.stderr)
    raise SystemExit(70)


def canonical(value: object) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def digest(path: Path) -> str:
    before = path.lstat()
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        opened = os.fstat(descriptor)
        identity = (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
        if (
            not stat.S_ISREG(opened.st_mode)
            or identity
            != (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        ):
            fail(f"unsafe or replaced path: {path}")
        value = hashlib.sha256()
        while chunk := os.read(descriptor, 1024 * 1024):
            value.update(chunk)
        after = os.fstat(descriptor)
        if identity != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            fail(f"path changed during secure read: {path}")
        return value.hexdigest()
    finally:
        os.close(descriptor)


try:
    status = json.loads(os.environ["RETHLAS_NONFRESH_STATUS_JSON"])
    cadence = json.loads(os.environ["RETHLAS_NONFRESH_CADENCE_JSON"])
    contract = json.loads(os.environ["RETHLAS_POLICY_CONTRACT_JSON"])
except (KeyError, json.JSONDecodeError, UnicodeError) as exc:
    fail(f"control output is not strict JSON: {exc}")
run_id = sys.argv[5]
required_status = {
    "active_turn_id",
    "generation",
    "problem_id",
    "quarantine",
    "run_id",
    "thread_id",
}
if not isinstance(status, dict) or not required_status <= set(status):
    fail("status projection omitted legacy run identity")
if status["run_id"] != run_id or not isinstance(status["problem_id"], str):
    fail("status projection is bound to a different run")
thread_id = status["thread_id"]
if not isinstance(thread_id, str) or not thread_id:
    fail("requested non-fresh resume has no existing thread")
if (
    not isinstance(status["generation"], int)
    or isinstance(status["generation"], bool)
    or status["generation"] < 0
):
    fail("status generation is malformed")
expected_cadence = {
    "context_guard",
    "continuous_supervisor",
    "disposition",
    "paid_turn_allowed",
    "quarantine",
    "review_cadence",
    "run_id",
    "thread_epoch",
}
if not isinstance(cadence, dict) or set(cadence) != expected_cadence:
    fail("cadence projection fields mismatch")
if (
    cadence["run_id"] != run_id
    or not isinstance(cadence["disposition"], str)
    or re.fullmatch(r"[a-z][a-z0-9_]{0,63}", cadence["disposition"]) is None
    or type(cadence["paid_turn_allowed"]) is not bool
):
    fail("cadence projection is malformed or bound to another run")
review_policy = contract.get("review_cadence_policy")
if not isinstance(review_policy, dict):
    fail("policy contract omitted review cadence")
guardian_ready = review_policy.get("guardian_enforcement_ready")
if type(guardian_ready) is not bool:
    fail("guardian release gate is malformed")

source = Path(sys.argv[1])
copy = Path(sys.argv[2])
source_before = sys.argv[3]
copy_before = sys.argv[4]
source_after = digest(source)
copy_after = digest(copy)
if source_after != source_before:
    fail("source DB changed during copied-ledger diagnosis")
source_stat = source.lstat()
copy_stat = copy.lstat()
if (
    (source_stat.st_dev, source_stat.st_ino) != (int(sys.argv[8]), int(sys.argv[9]))
    or (copy_stat.st_dev, copy_stat.st_ino)
    != (int(sys.argv[10]), int(sys.argv[11]))
):
    fail("source or copied DB inode changed during diagnosis")
if source.samefile(copy):
    fail("dry-run DB copy aliases the source after diagnosis")
if digest(Path(sys.argv[6])) != sys.argv[7]:
    fail("hot-join adapter changed during copied-ledger diagnosis")

disposition = cadence["disposition"]
if disposition == "stale_active":
    migration = "legacy_stale_active_offline_reconciliation_required"
    next_action = (
        "create another pristine owner-only copy and use the runtime's dedicated "
        "stale-recovery capability for one authenticated non-model thread/read receipt"
    )
elif disposition in {
    "hard_stopped_unfinalized",
    "owner_yield_close_required",
    "resume_active_cycle",
    "review_boundary_recovery_required",
    "terminal_observed_pending_finalization",
}:
    migration = "recovery_only_guardian_migration_required"
    next_action = (
        "after guardian release, obtain only the runtime's exact zero-model recovery receipt"
    )
elif disposition in {"hard_stopped", "route_frozen"}:
    migration = "normal_unsolved_terminal_no_resume"
    next_action = "preserve the terminal ledger; do not resume a paid turn"
elif disposition in {"operational_blocked", "execution_unknown"}:
    migration = "manual_ledger_migration_required"
    next_action = "perform an owner-side ledger audit; do not infer resume authority"
elif cadence["paid_turn_allowed"]:
    migration = "guardian_binding_required_before_nonfresh_resume"
    next_action = (
        "migrate the copied run into an authenticated guardian cycle before any resume"
    )
else:
    migration = "unknown_fail_closed_migration_required"
    next_action = "inspect the copied ledger with zero-model owner tooling"

diagnostic_reason = (
    "diagnostic only; explicit dry-run mode never authorizes paid or recovery execution"
    if guardian_ready
    else "diagnostic only; the copied legacy run has no released guardian authority "
    "for paid or recovery execution"
)
report = {
    "schema_version": "rethlas_nonfresh_resume_dry_run_v1",
    "diagnostic": "copied_legacy_ledger_nonfresh_resume",
    "run_id": run_id,
    "problem_id": status["problem_id"],
    "source_db": {
        "path": str(source),
        "sha256_before": source_before,
        "sha256_after": source_after,
        "unchanged": True,
    },
    "copy_db": {
        "path": str(copy),
        "sha256_before": copy_before,
        "sha256_after": copy_after,
        "schema_or_scheduler_migrated": copy_before != copy_after,
    },
    "policy": {
        "contract_sha256": contract["contract_sha256"],
        "guardian_enforcement_ready": guardian_ready,
    },
    "observed": {
        "thread_id": thread_id,
        "active_turn_id": status["active_turn_id"],
        "generation": status["generation"],
        "cadence_disposition": disposition,
        "paid_turn_allowed": cadence["paid_turn_allowed"],
        "quarantine": cadence["quarantine"],
    },
    "decision": {
        "requested_topology": "reuse_existing_thread",
        "existing_thread_preserved": True,
        "fresh_thread_forced_by_dry_run": False,
        "resume_admitted": False,
        "paid_processes_started": False,
        "recovery_migration_disposition": migration,
        "next_zero_model_action": next_action,
        "reason": diagnostic_reason,
    },
}
print(canonical(report))
PY
  )"; then
    echo "Copied-ledger non-fresh resume diagnosis failed closed." >&2
    exit 70
  fi
  echo "Non-fresh resume dry-run completed on a disposable DB copy; no Codex, reviewer, recovery, or paid control action was started, and any schema projection mutation was confined to the copy." >&2
  printf '%s\n' "$nonfresh_report"
  exit 0
  fi
  # The legacy reconcile exception is narrower than owner control: its fresh
  # token is scoped to this exact source/copy/thread/turn tuple, cannot
  # authenticate cadence or review operations, and is revoked by the terminal
  # receipt. Never inherit a master, guardian, runner, reasoning, or generation
  # capability into either owner-side control process.
  unset RETHLAS_REVIEW_CONTROL_TOKEN
  unset RETHLAS_GUARDIAN_CYCLE_TOKEN
  unset RETHLAS_RUNNER_CYCLE_TOKEN
  unset RETHLAS_GENERATION_CONTROL_TOKEN
  unset RETHLAS_STALE_RECOVERY_TOKEN
  if ! RETHLAS_POLICY_CONTRACT_JSON="$policy_contract_json" \
    "$TRUSTED_PYTHON_BIN" -I -B - <<'PY'
import json
import os

value = json.loads(os.environ["RETHLAS_POLICY_CONTRACT_JSON"])
if value["review_cadence_policy"]["guardian_enforcement_ready"] is not False:
    raise SystemExit(
        "stale-copy reconcile is only the explicit unreleased-guardian migration lane"
    )
PY
  then
    exit 70
  fi

  nonfresh_codex_command="$(command -v codex || true)"
  if [[ "$nonfresh_codex_command" != /* ]] \
    || [[ ! -x "$nonfresh_codex_command" ]]; then
    echo "Stale recovery requires one absolute attested Codex app-server executable." >&2
    exit 70
  fi
  if ! NONFRESH_CODEX_ATTESTATION_JSON="$(
    "$TRUSTED_PYTHON_BIN" -I -B - "$nonfresh_codex_command" <<'PY'
import hashlib
import json
import os
import stat
import sys
from pathlib import Path


def fail(message: str) -> None:
    raise SystemExit(f"unsafe stale-recovery Codex executable: {message}")


source = Path(os.path.abspath(sys.argv[1]))
try:
    target = source.resolve(strict=True)
    before = target.lstat()
except (OSError, RuntimeError) as exc:
    fail(f"cannot resolve executable: {exc}")
if target.is_symlink() or not stat.S_ISREG(before.st_mode):
    fail("resolved target must be a regular non-symlink file")
if stat.S_IMODE(before.st_mode) & 0o022:
    fail("resolved target must not be group/world-writable")
allowed_uids = {0, os.geteuid()}
if before.st_uid not in allowed_uids or not os.access(target, os.X_OK):
    fail("resolved target has unsafe ownership or is not executable")
descriptor = os.open(
    target,
    os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
)
try:
    opened = os.fstat(descriptor)
    identity = (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
    if identity != (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns):
        fail("resolved target changed while opened")
    digest = hashlib.sha256()
    while chunk := os.read(descriptor, 65536):
        digest.update(chunk)
    after = os.fstat(descriptor)
    if identity != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
        fail("resolved target changed while hashed")
finally:
    os.close(descriptor)
print(json.dumps(
    {"resolved_path": str(target), "sha256": digest.hexdigest()},
    allow_nan=False,
    sort_keys=True,
    separators=(",", ":"),
))
PY
  )"; then
    exit 70
  fi
  NONFRESH_CODEX_BIN="$(
    RETHLAS_CODEX_ATTESTATION_JSON="$NONFRESH_CODEX_ATTESTATION_JSON" \
      "$TRUSTED_PYTHON_BIN" -I -B -c \
        'import json, os; print(json.loads(os.environ["RETHLAS_CODEX_ATTESTATION_JSON"])["resolved_path"])'
  )" || exit 70
  NONFRESH_CODEX_BIN_SHA256="$(
    RETHLAS_CODEX_ATTESTATION_JSON="$NONFRESH_CODEX_ATTESTATION_JSON" \
      "$TRUSTED_PYTHON_BIN" -I -B -c \
        'import json, os; print(json.loads(os.environ["RETHLAS_CODEX_ATTESTATION_JSON"])["sha256"])'
  )" || exit 70
  NONFRESH_STALE_RECOVERY_TOKEN="$(
    "$TRUSTED_PYTHON_BIN" -I -B -c 'import secrets; print(secrets.token_hex(32))'
  )" || exit 70
  run_nonfresh_recovery_adapter() {
    RETHLAS_STALE_RECOVERY_TOKEN="$NONFRESH_STALE_RECOVERY_TOKEN" \
      "$TRUSTED_PYTHON_BIN" -I -B -c "$NONFRESH_ADAPTER_LOADER" \
        "$HOTJOIN_ADAPTER" "$NONFRESH_ADAPTER_SHA256" "$@"
  }

  nonfresh_prepare_envelope="$(
    "$TRUSTED_PYTHON_BIN" -I -B - \
      "$RETHLAS_HOTJOIN_RUN_ID" "$NONFRESH_EXPECTED_THREAD_ID" \
      "$NONFRESH_EXPECTED_TURN_ID" "$HOTJOIN_DB_DEFAULT" \
      "$NONFRESH_SOURCE_DB_SHA256_BEFORE" \
      "$NONFRESH_SOURCE_PREIMAGE_MANIFEST_SHA256" \
      "$NONFRESH_COPY_DB_DEVICE" "$NONFRESH_COPY_DB_INODE" \
      "$NONFRESH_COPY_DB_SHA256_BEFORE" "$NONFRESH_DB_OWNER_UID" \
      "$NONFRESH_CODEX_BIN" \
      "$NONFRESH_CODEX_BIN_SHA256" <<'PY'
import json
import sys

print(json.dumps({
    "schema_version": "rethlas_review_adapter_command_v1",
    "command": "stale_recovery_capability_prepare",
    "payload": {
        "operation": "stale_recovery_capability_prepare",
        "run_id": sys.argv[1],
        "expected_thread_id": sys.argv[2],
        "expected_turn_id": sys.argv[3],
        "source_database_path": sys.argv[4],
        "source_database_sha256": sys.argv[5],
        "source_preimage_manifest_sha256": sys.argv[6],
        "copy_database_device": int(sys.argv[7]),
        "copy_database_inode": int(sys.argv[8]),
        "copy_database_preimage_sha256": sys.argv[9],
        "owner_uid": int(sys.argv[10]),
        "database_mode_octal": "0600",
        "codex_bin": sys.argv[11],
        "codex_bin_sha256": sys.argv[12],
    },
}, allow_nan=False, sort_keys=True, separators=(",", ":")))
PY
  )" || exit 70
  if ! nonfresh_prepare_result_json="$(
    run_nonfresh_recovery_adapter --db "$HOTJOIN_DB" \
      stale-recovery-capability-prepare <<<"$nonfresh_prepare_envelope"
  )"; then
    echo "Dedicated stale-turn recovery capability preparation failed closed." >&2
    exit 70
  fi
  if ! RETHLAS_NONFRESH_PREPARE_RESULT_JSON="$nonfresh_prepare_result_json" \
    "$TRUSTED_PYTHON_BIN" -I -B - \
      "$RETHLAS_HOTJOIN_RUN_ID" "$NONFRESH_EXPECTED_THREAD_ID" \
      "$NONFRESH_EXPECTED_TURN_ID" "$NONFRESH_SOURCE_DB_SHA256_BEFORE" \
      "$NONFRESH_SOURCE_PREIMAGE_MANIFEST_SHA256" \
      "$NONFRESH_SOURCE_WAL_SIZE" "$NONFRESH_SOURCE_SHM_SIZE" \
      "$NONFRESH_COPY_DB_DEVICE" "$NONFRESH_COPY_DB_INODE" \
      "$NONFRESH_COPY_DB_SHA256_BEFORE" "$NONFRESH_CODEX_BIN" \
      "$NONFRESH_CODEX_BIN_SHA256" <<'PY'
import hashlib
import json
import os
import re
import sys


def fail(message: str) -> None:
    raise SystemExit(f"invalid stale recovery capability receipt: {message}")


def canonical(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


try:
    value = json.loads(os.environ["RETHLAS_NONFRESH_PREPARE_RESULT_JSON"])
except (KeyError, json.JSONDecodeError, UnicodeError) as exc:
    fail(f"not strict JSON: {exc}")
fields = {
    "schema_version",
    "operation",
    "capability_id",
    "run_id",
    "state",
    "scope",
    "expected_thread_id",
    "expected_turn_id",
    "source_database_sha256",
    "source_preimage_manifest_sha256",
    "source_sidecars",
    "backup_manifest_sha256",
    "copy_database_device",
    "copy_database_inode",
    "copy_database_preimage_sha256",
    "codex_bin",
    "codex_bin_sha256",
    "created_sequence",
    "receipt_sha256",
}
if not isinstance(value, dict) or set(value) != fields:
    fail("fields mismatch")
if (
    value["schema_version"] != "rethlas_stale_recovery_capability_v1"
    or value["operation"] != "stale_recovery_capability_prepare"
    or re.fullmatch(r"stalecap_[0-9a-f]{32}", value.get("capability_id", ""))
    is None
    or value["run_id"] != sys.argv[1]
    or value["state"] != "active"
    or value["scope"] != "stale_turn_reconcile"
    or value["expected_thread_id"] != sys.argv[2]
    or value["expected_turn_id"] != sys.argv[3]
    or value["source_database_sha256"] != sys.argv[4]
    or value["source_preimage_manifest_sha256"] != sys.argv[5]
    or value["source_sidecars"]
    != {"wal_size": int(sys.argv[6]), "shm_size": int(sys.argv[7])}
    or value["copy_database_device"] != int(sys.argv[8])
    or value["copy_database_inode"] != int(sys.argv[9])
    or value["copy_database_preimage_sha256"] != sys.argv[10]
    or value["codex_bin"] != sys.argv[11]
    or value["codex_bin_sha256"] != sys.argv[12]
    or type(value["created_sequence"]) is not int
    or value["created_sequence"] < 1
):
    fail("durable target or provenance binding mismatch")
for field in (
    "source_preimage_manifest_sha256",
    "backup_manifest_sha256",
    "receipt_sha256",
):
    if re.fullmatch(r"[0-9a-f]{64}", value.get(field, "")) is None:
        fail(f"{field} is malformed")
seed = dict(value)
receipt = seed.pop("receipt_sha256")
if hashlib.sha256(canonical(seed)).hexdigest() != receipt:
    fail("receipt SHA-256 mismatch")
PY
  then
    exit 70
  fi
  if ! nonfresh_initial_status_json="$(
    run_nonfresh_adapter --db "$HOTJOIN_DB" status \
      --run-id "$RETHLAS_HOTJOIN_RUN_ID"
  )" || ! nonfresh_initial_cadence_json="$(
    run_nonfresh_adapter --db "$HOTJOIN_DB" cadence-control-state \
      --run-id "$RETHLAS_HOTJOIN_RUN_ID"
  )"; then
    echo "Could not inspect the exact legacy orphan before reconciliation." >&2
    exit 70
  fi
  if ! RETHLAS_NONFRESH_STATUS_JSON="$nonfresh_initial_status_json" \
    RETHLAS_NONFRESH_CADENCE_JSON="$nonfresh_initial_cadence_json" \
    RETHLAS_POLICY_CONTRACT_JSON="$policy_contract_json" \
      "$TRUSTED_PYTHON_BIN" -I -B - \
        "$RETHLAS_HOTJOIN_RUN_ID" "$NONFRESH_EXPECTED_THREAD_ID" \
        "$NONFRESH_EXPECTED_TURN_ID" <<'PY'
import json
import os
import sys


def fail(message: str) -> None:
    print(f"stale-turn recovery preflight rejected: {message}", file=sys.stderr)
    raise SystemExit(70)


try:
    status = json.loads(os.environ["RETHLAS_NONFRESH_STATUS_JSON"])
    cadence = json.loads(os.environ["RETHLAS_NONFRESH_CADENCE_JSON"])
    contract = json.loads(os.environ["RETHLAS_POLICY_CONTRACT_JSON"])
except (KeyError, json.JSONDecodeError, UnicodeError) as exc:
    fail(f"control output is not strict JSON: {exc}")
run_id, thread_id, turn_id = sys.argv[1:4]
if (
    not isinstance(status, dict)
    or status.get("run_id") != run_id
    or status.get("thread_id") != thread_id
    or status.get("active_turn_id") != turn_id
    or not isinstance(status.get("problem_id"), str)
    or not status["problem_id"]
):
    fail("status does not bind the exact legacy run/thread/turn")
if not isinstance(status.get("generation"), int) or isinstance(
    status["generation"], bool
):
    fail("legacy generation is malformed")
expected = {
    "context_guard",
    "continuous_supervisor",
    "disposition",
    "paid_turn_allowed",
    "quarantine",
    "review_cadence",
    "run_id",
    "thread_epoch",
}
if not isinstance(cadence, dict) or set(cadence) != expected:
    fail("cadence projection fields mismatch")
review = cadence.get("review_cadence")
context = cadence.get("context_guard")
if (
    cadence.get("run_id") != run_id
    or cadence.get("disposition") != "stale_active"
    or cadence.get("paid_turn_allowed") is not False
    or cadence.get("quarantine") is not None
    or cadence.get("continuous_supervisor") is not None
    or not isinstance(review, dict)
    or not isinstance(context, dict)
    or context.get("adapter_resume_allowed") is not False
    or review.get("policy_id")
    != contract["review_cadence_policy"]["policy_id"]
    or review.get("policy_digest")
    != contract["review_cadence_policy"]["policy_sha256"]
    or context.get("policy_id") != contract["context_guard_policy"]["policy_id"]
    or context.get("policy_digest")
    != contract["context_guard_policy"]["policy_sha256"]
):
    fail("copy is not one paid-disabled legacy stale orphan")
PY
  then
    exit 70
  fi


  nonfresh_reconcile_envelope="$(
    "$TRUSTED_PYTHON_BIN" -I -B - \
      "$RETHLAS_HOTJOIN_RUN_ID" "$NONFRESH_EXPECTED_THREAD_ID" \
      "$NONFRESH_EXPECTED_TURN_ID" <<'PY'
import json
import sys

print(json.dumps({
    "schema_version": "rethlas_review_adapter_command_v1",
    "command": "stale_turn_reconcile",
    "payload": {
        "operation": "stale_turn_reconcile",
        "run_id": sys.argv[1],
        "expected_thread_id": sys.argv[2],
        "expected_turn_id": sys.argv[3],
    },
}, allow_nan=False, sort_keys=True, separators=(",", ":")))
PY
  )" || exit 70
  if ! nonfresh_reconcile_result_json="$(
    run_nonfresh_recovery_adapter --db "$HOTJOIN_DB" stale-turn-reconcile \
      <<<"$nonfresh_reconcile_envelope"
  )"; then
    echo "Authenticated read-only stale-turn reconciliation failed closed." >&2
    exit 70
  fi
  NONFRESH_STALE_RECOVERY_TOKEN=""
  unset NONFRESH_STALE_RECOVERY_TOKEN
  if ! nonfresh_post_status_json="$(
    run_nonfresh_adapter --db "$HOTJOIN_DB" status \
      --run-id "$RETHLAS_HOTJOIN_RUN_ID"
  )" || ! nonfresh_post_cadence_json="$(
    run_nonfresh_adapter --db "$HOTJOIN_DB" cadence-control-state \
      --run-id "$RETHLAS_HOTJOIN_RUN_ID"
  )"; then
    echo "Could not project the copied ledger after reconciliation." >&2
    exit 70
  fi
  if ! nonfresh_reconcile_report="$(
    RETHLAS_NONFRESH_INITIAL_CADENCE_JSON="$nonfresh_initial_cadence_json" \
    RETHLAS_NONFRESH_PREPARE_RESULT_JSON="$nonfresh_prepare_result_json" \
    RETHLAS_NONFRESH_RECONCILE_RESULT_JSON="$nonfresh_reconcile_result_json" \
    RETHLAS_NONFRESH_POST_STATUS_JSON="$nonfresh_post_status_json" \
    RETHLAS_NONFRESH_POST_CADENCE_JSON="$nonfresh_post_cadence_json" \
      "$TRUSTED_PYTHON_BIN" -I -B - \
        "$HOTJOIN_DB_DEFAULT" "$HOTJOIN_DB" \
        "$NONFRESH_SOURCE_DB_SHA256_BEFORE" \
        "$NONFRESH_COPY_DB_SHA256_BEFORE" "$RETHLAS_HOTJOIN_RUN_ID" \
        "$NONFRESH_EXPECTED_THREAD_ID" "$NONFRESH_EXPECTED_TURN_ID" \
		"$HOTJOIN_ADAPTER" "$NONFRESH_ADAPTER_SHA256" \
		"$NONFRESH_SOURCE_DB_DEVICE" "$NONFRESH_SOURCE_DB_INODE" \
		"$NONFRESH_COPY_DB_DEVICE" "$NONFRESH_COPY_DB_INODE" \
		"$NONFRESH_SOURCE_WAL_MANIFEST" \
		"$NONFRESH_SOURCE_SHM_MANIFEST" \
		"$NONFRESH_SOURCE_PREIMAGE_MANIFEST" \
		"$NONFRESH_SOURCE_PREIMAGE_MANIFEST_SHA256" <<'PY'
import hashlib
import json
import os
import re
import stat
import sys
from pathlib import Path


def fail(message: str) -> None:
    print(f"invalid stale-turn reconcile receipt: {message}", file=sys.stderr)
    raise SystemExit(70)


def canonical(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def secure_digest(path: Path) -> str:
    before = path.lstat()
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        opened = os.fstat(descriptor)
        identity = (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
        if (
            not stat.S_ISREG(opened.st_mode)
            or identity
            != (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        ):
            fail(f"unsafe or replaced path: {path}")
        digest = hashlib.sha256()
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
        after = os.fstat(descriptor)
        if identity != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            fail(f"path changed during secure read: {path}")
        return digest.hexdigest()
    finally:
        os.close(descriptor)


def secure_sidecar_manifest(path: Path, label: str) -> dict[str, object]:
    try:
        before = path.lstat()
    except FileNotFoundError:
        return {"present": False}
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
            fail(f"{label} changed before final secure read")
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_uid != os.getuid()
            or opened.st_nlink != 1
            or opened.st_size > 64 * 1024 * 1024
            or stat.S_IMODE(opened.st_mode) & 0o077
        ):
            fail(f"{label} is not one owner-only regular file")
        digest = hashlib.sha256()
        remaining = opened.st_size
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                fail(f"{label} was truncated during final secure read")
            digest.update(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            fail(f"{label} grew during final secure read")
        after = os.fstat(descriptor)
        path_after = path.lstat()
        if identity != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ) or identity != (
            path_after.st_dev,
            path_after.st_ino,
            path_after.st_size,
            path_after.st_mtime_ns,
        ):
            fail(f"{label} changed during final secure read")
        return {
            "present": True,
            "device": opened.st_dev,
            "inode": opened.st_ino,
            "owner_uid": opened.st_uid,
            "mode_octal": f"{stat.S_IMODE(opened.st_mode):04o}",
            "size": opened.st_size,
            "mtime_ns": opened.st_mtime_ns,
            "sha256": digest.hexdigest(),
        }
    finally:
        os.close(descriptor)


try:
    initial = json.loads(os.environ["RETHLAS_NONFRESH_INITIAL_CADENCE_JSON"])
    prepare = json.loads(os.environ["RETHLAS_NONFRESH_PREPARE_RESULT_JSON"])
    result = json.loads(os.environ["RETHLAS_NONFRESH_RECONCILE_RESULT_JSON"])
    status = json.loads(os.environ["RETHLAS_NONFRESH_POST_STATUS_JSON"])
    cadence = json.loads(os.environ["RETHLAS_NONFRESH_POST_CADENCE_JSON"])
except (KeyError, json.JSONDecodeError, UnicodeError) as exc:
    fail(f"control output is not strict JSON: {exc}")
run_id, thread_id, turn_id = sys.argv[5:8]
fields = {
    "schema_version",
    "operation",
    "run_id",
    "thread_id",
    "turn_id",
    "state",
    "observed_status",
    "thread_read_response_sha256",
    "turn_sha256",
    "terminal_sha256",
    "settled_message_count",
    "settled_messages_sha256",
    "committed_sequence",
    "receipt_sha256",
}
if not isinstance(result, dict) or set(result) != fields:
    fail("result fields mismatch")
if (
    result["schema_version"] != "rethlas_stale_turn_reconcile_result_v1"
    or result["operation"] != "stale_turn_reconcile"
    or (result["run_id"], result["thread_id"], result["turn_id"])
    != (run_id, thread_id, turn_id)
):
    fail("result target binding mismatch")
for name in (
    "thread_read_response_sha256",
    "settled_messages_sha256",
    "receipt_sha256",
):
    if re.fullmatch(r"[0-9a-f]{64}", result.get(name, "")) is None:
        fail(f"{name} is malformed")
for name in ("turn_sha256", "terminal_sha256"):
    value = result.get(name)
    if value is not None and re.fullmatch(r"[0-9a-f]{64}", value) is None:
        fail(f"{name} is malformed")
if (
    type(result.get("settled_message_count")) is not int
    or result["settled_message_count"] < 0
    or type(result.get("committed_sequence")) is not int
    or result["committed_sequence"] < 1
):
    fail("result counts are malformed")
seed = dict(result)
receipt = seed.pop("receipt_sha256")
if hashlib.sha256(canonical(seed)).hexdigest() != receipt:
    fail("result receipt SHA-256 mismatch")
state = result["state"]
if state == "terminal_reconciled_quarantined":
    quarantine = cadence.get("quarantine")
    if (
        result["observed_status"] not in {"completed", "interrupted", "failed"}
        or result["turn_sha256"] is None
        or result["terminal_sha256"] is None
        or status.get("active_turn_id") is not None
        or cadence.get("disposition") != "operational_blocked"
        or cadence.get("paid_turn_allowed") is not False
        or not isinstance(quarantine, dict)
        or quarantine.get("kind") != "adapter_loss_terminal_discontinuity"
    ):
        fail("terminal result did not converge into immutable quarantine")
    candidate = {
        "eligible": True,
        "source_terminal_sha256": result["terminal_sha256"],
        "source_thread_read_response_sha256": result[
            "thread_read_response_sha256"
        ],
        "use": "host_may_extract_one_bounded_handoff_candidate_from_quarantined_thread_read",
        "resume_authority": False,
    }
elif state == "guardian_interrupt_intent_required":
    if (
        result["observed_status"] != "inProgress"
        or result["turn_sha256"] is None
        or result["terminal_sha256"] is not None
        or status.get("active_turn_id") != turn_id
        or cadence.get("disposition")
        != "stale_turn_guardian_interrupt_required"
        or cadence.get("paid_turn_allowed") is not False
        or cadence.get("quarantine") is not None
    ):
        fail("in-progress result did not freeze at guardian interrupt intent")
    candidate = {
        "eligible": False,
        "source_terminal_sha256": None,
        "source_thread_read_response_sha256": result[
            "thread_read_response_sha256"
        ],
        "use": "await_guardian_interrupt_and_authenticated_terminal_receipt",
        "resume_authority": False,
    }
elif state == "operational_blocked":
    if (
        result["terminal_sha256"] is not None
        or cadence.get("disposition") != "operational_blocked"
        or cadence.get("paid_turn_allowed") is not False
    ):
        fail("ambiguous result did not fail closed")
    candidate = {
        "eligible": False,
        "source_terminal_sha256": None,
        "source_thread_read_response_sha256": result[
            "thread_read_response_sha256"
        ],
        "use": "manual_owner_audit_only",
        "resume_authority": False,
    }
else:
    fail("unsupported reconciliation state")
if (
    status.get("run_id") != run_id
    or status.get("thread_id") != thread_id
    or cadence.get("run_id") != run_id
    or initial.get("disposition") != "stale_active"
):
    fail("post-reconcile target mismatch")
source, copy = Path(sys.argv[1]), Path(sys.argv[2])
source_before, copy_before = sys.argv[3:5]
source_after, copy_after = secure_digest(source), secure_digest(copy)
source_stat = source.lstat()
copy_stat = copy.lstat()
try:
    expected_wal_manifest = json.loads(sys.argv[14])
    expected_shm_manifest = json.loads(sys.argv[15])
    expected_source_manifest = json.loads(sys.argv[16])
except (json.JSONDecodeError, UnicodeError) as exc:
    fail(f"initial source sidecar manifest is malformed: {exc}")
current_wal_manifest = secure_sidecar_manifest(
    Path(str(source) + "-wal"), "source WAL sidecar"
)
current_shm_manifest = secure_sidecar_manifest(
    Path(str(source) + "-shm"), "source SHM sidecar"
)
current_source_manifest = {
    "schema_version": "rethlas_recovery_source_preimage_v1",
    "database": {
        "device": source_stat.st_dev,
        "inode": source_stat.st_ino,
        "owner_uid": source_stat.st_uid,
        "mode_octal": f"{stat.S_IMODE(source_stat.st_mode):04o}",
        "size": source_stat.st_size,
        "mtime_ns": source_stat.st_mtime_ns,
        "sha256": source_after,
    },
    "wal": current_wal_manifest,
    "shm": current_shm_manifest,
}
expected_source_manifest_sha256 = sys.argv[17]
if (
    re.fullmatch(r"[0-9a-f]{64}", expected_source_manifest_sha256) is None
    or hashlib.sha256(canonical(expected_source_manifest)).hexdigest()
    != expected_source_manifest_sha256
    or prepare.get("source_preimage_manifest_sha256")
    != expected_source_manifest_sha256
):
    fail("source preimage manifest commitment is malformed")
if (
    source_after != source_before
    or source.samefile(copy)
    or source_stat.st_nlink != 1
    or source_stat.st_uid != os.getuid()
    or stat.S_IMODE(source_stat.st_mode) != 0o600
    or copy_stat.st_nlink != 1
    or copy_stat.st_uid != os.getuid()
    or stat.S_IMODE(copy_stat.st_mode) != 0o600
    or (source_stat.st_dev, source_stat.st_ino)
    != (int(sys.argv[10]), int(sys.argv[11]))
    or (copy_stat.st_dev, copy_stat.st_ino)
    != (int(sys.argv[12]), int(sys.argv[13]))
):
    fail("authoritative DB changed or copy aliases it")
if (
    current_wal_manifest != expected_wal_manifest
    or current_shm_manifest != expected_shm_manifest
    or current_source_manifest != expected_source_manifest
):
    fail("authoritative DB preimage manifest changed during reconciliation")
if secure_digest(Path(sys.argv[8])) != sys.argv[9]:
    fail("hot-join adapter changed during reconciliation")
report = {
    "schema_version": "rethlas_nonfresh_stale_reconcile_report_v1",
    "run_id": run_id,
    "thread_id": thread_id,
    "turn_id": turn_id,
    "source_db": {
        "path": str(source),
        "sha256_before": source_before,
        "sha256_after": source_after,
        "preimage_manifest_sha256": expected_source_manifest_sha256,
        "sidecars": {
            "wal": current_wal_manifest,
            "shm": current_shm_manifest,
        },
        "unchanged": True,
    },
    "copy_db": {
        "path": str(copy),
        "sha256_before": copy_before,
        "sha256_after": copy_after,
        "mutated_by_reconciliation": copy_before != copy_after,
    },
    "capability_receipt_sha256": prepare["receipt_sha256"],
    "initial_disposition": initial.get("disposition"),
    "reconcile_result": result,
    "post_disposition": cadence.get("disposition"),
    "handoff_candidate": candidate,
    "decision": {
        "resume_admitted": False,
        "fresh_thread_started": False,
        "model_calls_started": 0,
        "paid_turns_started": 0,
        "read_only_app_server_processes_started": 1,
        "read_only_app_server_calls": ["initialize", "thread/read"],
        "next_action": candidate["use"],
    },
}
print(json.dumps(
    report,
    allow_nan=False,
    ensure_ascii=False,
    sort_keys=True,
    separators=(",", ":"),
))
PY
  )"; then
    echo "Stale-turn reconciliation receipt/projection validation failed closed." >&2
    exit 70
  fi
  printf '%s\n' "$nonfresh_reconcile_report"
  echo "Stale-turn reconciliation completed only on the disposable copy: one pinned read-only app-server performed initialize+thread/read, zero model/paid turns/reviewers/verifiers ran, and no resume authority was created." >&2
  exit 70
fi

if [[ "$PROBLEM_FILE" = /* ]]; then
  echo "PROBLEM_FILE must be relative to agents/generation: $PROBLEM_FILE" >&2
  exit 1
fi

if [[ "$PROBLEM_FILE" == ".." || "$PROBLEM_FILE" == ../* || "$PROBLEM_FILE" == */.. || "$PROBLEM_FILE" == */../* ]]; then
  echo "PROBLEM_FILE must not contain '..': $PROBLEM_FILE" >&2
  exit 1
fi

if [[ "$PROBLEM_FILE" != data/*.md ]]; then
  echo "PROBLEM_FILE must point to a markdown file under data/: $PROBLEM_FILE" >&2
  exit 1
fi

if [[ ! -f "$ROOT_DIR/$PROBLEM_FILE" ]]; then
  echo "Problem file not found: $ROOT_DIR/$PROBLEM_FILE" >&2
  exit 1
fi

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
RETHLAS_GENERATION_CONTROL_TOKEN="$("$TRUSTED_PYTHON_BIN" -I -B -c \
  'import secrets; print(secrets.token_hex(16))')"
export RETHLAS_GENERATION_CONTROL_TOKEN
# Never inherit an ambient privileged capability.  Unsetting first also clears
# the shell export attribute, so the fresh owner token below stays memory-local
# to this wrapper unless it is explicitly framed into a one-shot FIFO.
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
ACTIVE_LOG_DIR="$LOG_DIR"
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
receipt_path="$RETHLAS_RECEIPTS_ROOT/$problem_rel.json"
mkdir -p "$LOG_DIR"

# A wrapper-local ordinal is useful UI, but it is not a durable identity.  For
# guarded hot-join runs, isolate every wrapper invocation under the fresh,
# owner-bound generation-control instance so a restart cannot truncate an old
# ``*_iter_0.md``.  The protected log root sits outside the model-writable
# generation workspace beside the durable ledger.
if [[ -n "$RETHLAS_HOTJOIN_RUN_ID" \
   && "$REVIEW_CADENCE_POLICY" != disabled ]]; then
  ACTIVE_LOG_DIR="$(dirname "$HOTJOIN_DB")/logs/$RETHLAS_HOTJOIN_RUN_ID/invocation_$RETHLAS_GENERATION_CONTROL_TOKEN"
  if ! "$TRUSTED_PYTHON_BIN" -I -B - "$ACTIVE_LOG_DIR" <<'PY'
import os
import re
import stat
import sys
from pathlib import Path

target = Path(sys.argv[1])
if not target.is_absolute() or re.fullmatch(r"invocation_[0-9a-f]{32}", target.name) is None:
    raise SystemExit("guarded log invocation path is malformed")
missing: list[Path] = []
cursor = target
while not cursor.exists():
    if cursor.is_symlink():
        raise SystemExit(f"guarded log path is a symlink: {cursor}")
    missing.append(cursor)
    cursor = cursor.parent
while True:
    metadata = cursor.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise SystemExit(f"guarded log ancestor is unsafe: {cursor}")
    if cursor.parent == cursor:
        break
    cursor = cursor.parent
for path in reversed(missing):
    path.mkdir(mode=0o700)
    os.chmod(path, 0o700)
metadata = target.lstat()
if (
    not stat.S_ISDIR(metadata.st_mode)
    or stat.S_IMODE(metadata.st_mode) != 0o700
    or (hasattr(os, "getuid") and metadata.st_uid != os.getuid())
):
    raise SystemExit("guarded log invocation directory is not owner-only")
PY
  then
    echo "Could not create a fresh owner-only guarded log invocation directory." >&2
    exit 70
  fi
fi

trusted_runtime_manifest() {
  local manifest_root="${1:-$ROOT_DIR}"
  local review_root="${2:-$ROOT_DIR/../review}"
  local runner_source="${3:-$GUARDIAN_RUNNER}"
  "$TRUSTED_PYTHON_BIN" -I -B - \
    "$manifest_root" "$review_root" "$runner_source" <<'PY'
import hashlib
import os
import stat
import sys
from pathlib import Path

if not sys.flags.isolated or not sys.dont_write_bytecode:
    raise SystemExit("trusted runtime manifest requires Python -I -B")

root = Path(sys.argv[1]).resolve(strict=True)
review_input = Path(sys.argv[2]).absolute()
if review_input.is_symlink():
    print(f"unsafe trusted generation runtime: review root is a symlink: {review_input}", file=sys.stderr)
    raise SystemExit(2)
review_root = review_input.resolve(strict=True)
runner_source = Path(sys.argv[3]).resolve(strict=True)
explicit = [
    (root / "AGENTS.md", Path("AGENTS.md")),
    (root / "requirements-math-research.txt", Path("requirements-math-research.txt")),
    (runner_source, Path("tests/run_hotjoin.sh")),
]
trees = [
    (root / ".codex", Path(".codex")),
    (root / ".agents", Path(".agents")),
    (root / "mcp", Path("mcp")),
    (review_root, Path("review")),
]

def fail(message: str) -> None:
    print(f"unsafe trusted generation runtime: {message}", file=sys.stderr)
    raise SystemExit(2)


entries: list[tuple[str, Path, Path, os.stat_result]] = []
for path, logical_path in explicit:
    try:
        metadata = path.lstat()
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
        with path.open("rb") as handle:
            while chunk := handle.read(65536):
                digest.update(chunk)
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
trusted_runtime_dir="$(mktemp -d "$trusted_runtime_parent/runtime.XXXXXX")"
mkdir -p "$trusted_runtime_dir/tests"
cp -p "$ROOT_DIR/AGENTS.md" "$trusted_runtime_dir/AGENTS.md"
cp -p "$ROOT_DIR/requirements-math-research.txt" \
  "$trusted_runtime_dir/requirements-math-research.txt"
cp -p "$GUARDIAN_RUNNER" "$trusted_runtime_dir/tests/run_hotjoin.sh"
cp -pR "$ROOT_DIR/.codex" "$trusted_runtime_dir/.codex"
cp -pR "$ROOT_DIR/.agents" "$trusted_runtime_dir/.agents"
cp -pR "$ROOT_DIR/mcp" "$trusted_runtime_dir/mcp"
cp -pR "$ROOT_DIR/../review" "$trusted_runtime_dir/review"
SNAPSHOT_RUNTIME_MANIFEST="$(
  trusted_runtime_manifest "$trusted_runtime_dir" "$trusted_runtime_dir/review" \
    "$trusted_runtime_dir/tests/run_hotjoin.sh"
)" || {
  echo "Could not attest the trusted generation runtime snapshot." >&2
  exit 1
}
if [[ "$SNAPSHOT_RUNTIME_MANIFEST" != "$TRUSTED_RUNTIME_MANIFEST" ]]; then
  echo "Trusted generation runtime changed while its snapshot was created." >&2
  exit 70
fi
REVIEW_CONTRACT_CLI_PATH="$trusted_runtime_dir/review/contract_cli.py"
if [[ ! -f "$REVIEW_CONTRACT_CLI_PATH" || -L "$REVIEW_CONTRACT_CLI_PATH" ]]; then
  echo "Trusted review contract CLI must be a regular non-symlink file: $REVIEW_CONTRACT_CLI_PATH" >&2
  exit 70
fi
REVIEW_CONTRACT_CLI_SHA256="$(
  "$TRUSTED_PYTHON_BIN" -I -B -c \
    'import hashlib, pathlib, sys; print(hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest())' \
    "$REVIEW_CONTRACT_CLI_PATH"
)"
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

MCP_SERVER_PATH="$trusted_runtime_dir/mcp/server.py"
MCP_SERVER_DRIVER_PATH="$trusted_runtime_dir/mcp/server_driver.py"
MCP_ADVISOR_CLIENT_PATH="$trusted_runtime_dir/mcp/advisor_client.py"
MCP_PUBLICATION_PROOF_CONTEXT_PATH="$trusted_runtime_dir/mcp/publication_proof_context_v3.py"
MCP_PROOF_CONTEXT_PATH="$trusted_runtime_dir/mcp/proof_context.py"
MCP_REVIEW_CLIENT_PATH="$trusted_runtime_dir/mcp/review_client.py"
MCP_VERIFICATION_CLIENT_PATH="$trusted_runtime_dir/mcp/verification_client.py"
REVIEW_CONTRACTS_PATH="$trusted_runtime_dir/review/contracts.py"
REVIEW_CRITIC_PATH="$trusted_runtime_dir/review/critic.py"
MCP_SERVER_SHA256="$(attest_snapshot_module "$ROOT_DIR/mcp/server.py" "$MCP_SERVER_PATH")" || exit 70
MCP_SERVER_DRIVER_SHA256="$(attest_snapshot_module "$ROOT_DIR/mcp/server_driver.py" "$MCP_SERVER_DRIVER_PATH")" || exit 70
MCP_ADVISOR_CLIENT_SHA256="$(attest_snapshot_module "$ROOT_DIR/mcp/advisor_client.py" "$MCP_ADVISOR_CLIENT_PATH")" || exit 70
MCP_PUBLICATION_PROOF_CONTEXT_SHA256="$(attest_snapshot_module "$ROOT_DIR/mcp/publication_proof_context_v3.py" "$MCP_PUBLICATION_PROOF_CONTEXT_PATH")" || exit 70
MCP_PROOF_CONTEXT_SHA256="$(attest_snapshot_module "$ROOT_DIR/mcp/proof_context.py" "$MCP_PROOF_CONTEXT_PATH")" || exit 70
MCP_REVIEW_CLIENT_SHA256="$(attest_snapshot_module "$ROOT_DIR/mcp/review_client.py" "$MCP_REVIEW_CLIENT_PATH")" || exit 70
MCP_VERIFICATION_CLIENT_SHA256="$(attest_snapshot_module "$ROOT_DIR/mcp/verification_client.py" "$MCP_VERIFICATION_CLIENT_PATH")" || exit 70
REVIEW_CONTRACTS_SHA256="$(attest_snapshot_module "$ROOT_DIR/../review/contracts.py" "$REVIEW_CONTRACTS_PATH")" || exit 70
REVIEW_CRITIC_SHA256="$(attest_snapshot_module "$ROOT_DIR/../review/critic.py" "$REVIEW_CRITIC_PATH")" || exit 70

review_driver_package_sha256() {
  "$TRUSTED_PYTHON_BIN" -I -B - "$trusted_runtime_dir" <<'PY'
import hashlib
import json
import os
import stat
import sys
from pathlib import Path


def fail(message: str) -> None:
    print(f"trusted review-driver package attestation failed: {message}", file=sys.stderr)
    raise SystemExit(70)


root = Path(sys.argv[1]).absolute()
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
total_bytes = 0
for logical_path in logical_paths:
    relative = Path(logical_path)
    path = (
        root / "mcp" / relative.name
        if relative.parts[:2] == ("generation", "mcp")
        else root / "review" / relative.name
    )
    try:
        before = path.lstat()
    except OSError as exc:
        fail(f"cannot inspect {logical_path}: {exc}")
    if (
        path.is_symlink()
        or not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or before.st_size < 0
        or before.st_size > 8_000_000
        or (hasattr(os, "getuid") and before.st_uid != os.getuid())
        or stat.S_IMODE(before.st_mode) & 0o022
    ):
        fail(f"unsafe package entry: {logical_path}")
    total_bytes += int(before.st_size)
    if total_bytes > 32_000_000:
        fail("package exceeds 32 MB")
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        opened = os.fstat(descriptor)
        identity = (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
        if identity != (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ):
            fail(f"package entry changed during open: {logical_path}")
        digest = hashlib.sha256()
        remaining = int(opened.st_size)
        while remaining:
            chunk = os.read(descriptor, min(65_536, remaining))
            if not chunk:
                fail(f"package entry truncated: {logical_path}")
            digest.update(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            fail(f"package entry grew: {logical_path}")
        after = os.fstat(descriptor)
        if identity != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            fail(f"package entry changed during read: {logical_path}")
    finally:
        os.close(descriptor)
    entries.append(
        {"path": logical_path, "sha256": digest.hexdigest(), "size": before.st_size}
    )
manifest = {
    "schema_version": "rethlas_review_driver_package_v1",
    "files": sorted(entries, key=lambda item: item["path"]),
}
encoded = json.dumps(
    manifest,
    allow_nan=False,
    ensure_ascii=False,
    sort_keys=True,
    separators=(",", ":"),
).encode("utf-8")
print(hashlib.sha256(encoded).hexdigest())
PY
}

REVIEW_DRIVER_PATH="$MCP_SERVER_DRIVER_PATH"
REVIEW_DRIVER_SHA256="$MCP_SERVER_DRIVER_SHA256"
REVIEW_DRIVER_PACKAGE_SHA256="$(review_driver_package_sha256)" || exit 70
if [[ "$(trusted_runtime_manifest)" != "$TRUSTED_RUNTIME_MANIFEST" ]]; then
  echo "Trusted generation runtime changed during executable-module attestation." >&2
  exit 70
fi

TRUSTED_MCP_SECURE_LOADER="$(cat <<'PY'
import hashlib, hmac, importlib.util, os, re, stat, sys, types

EXPECTED = (
    "review.contracts",
    "review.critic",
    "mcp.publication_proof_context_v3",
    "mcp.proof_context",
    "mcp.advisor_client",
    "mcp.review_client",
    "mcp.verification_client",
    "mcp.server",
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


install_package("review")
local_mcp_package = "_rethlas_generation_mcp"
install_package(local_mcp_package)
for module_name in EXPECTED[:-1]:
    runtime_name = (
        local_mcp_package + module_name[len("mcp") :]
        if module_name.startswith("mcp.")
        else module_name
    )
    execute_module(module_name, runtime_name)
server = execute_module("mcp.server", local_mcp_package + ".server")
sys.argv = [captured["mcp.server"][0], *entry_arguments]
server.main()
PY
)"
TRUSTED_MCP_LOADER_ARGS=(
  -I -B -c "$TRUSTED_MCP_SECURE_LOADER"
  review.contracts "$REVIEW_CONTRACTS_PATH" "$REVIEW_CONTRACTS_SHA256"
  review.critic "$REVIEW_CRITIC_PATH" "$REVIEW_CRITIC_SHA256"
  mcp.publication_proof_context_v3 "$MCP_PUBLICATION_PROOF_CONTEXT_PATH" "$MCP_PUBLICATION_PROOF_CONTEXT_SHA256"
  mcp.proof_context "$MCP_PROOF_CONTEXT_PATH" "$MCP_PROOF_CONTEXT_SHA256"
  mcp.advisor_client "$MCP_ADVISOR_CLIENT_PATH" "$MCP_ADVISOR_CLIENT_SHA256"
  mcp.review_client "$MCP_REVIEW_CLIENT_PATH" "$MCP_REVIEW_CLIENT_SHA256"
  mcp.verification_client "$MCP_VERIFICATION_CLIENT_PATH" "$MCP_VERIFICATION_CLIENT_SHA256"
  mcp.server "$MCP_SERVER_PATH" "$MCP_SERVER_SHA256"
)
export RETHLAS_GENERATION_ROOT="$ROOT_DIR"
if [[ "$REVIEW_CADENCE_POLICY" != disabled ]]; then
  export RETHLAS_REVIEW_CONTRACT_CLI_PATH="$REVIEW_CONTRACT_CLI_PATH"
  export RETHLAS_REVIEW_CONTRACT_CLI_SHA256="$REVIEW_CONTRACT_CLI_SHA256"
else
  unset RETHLAS_REVIEW_CONTRACT_CLI_PATH
  unset RETHLAS_REVIEW_CONTRACT_CLI_SHA256
fi
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
  "$TRUSTED_PYTHON_BIN" "${TRUSTED_MCP_LOADER_ARGS[@]}" -- \
    --generation-control-resume "$problem_rel"
}

owner_memory_batch_publication_snapshot() {
  local envelope response canonical
  if [[ "$REVIEW_CADENCE_POLICY" == disabled ]]; then
    echo "Owner memory publication snapshots require the released cadence." >&2
    return 70
  fi
  envelope="$(
    "$TRUSTED_PYTHON_BIN" -I -B - "$problem_rel" <<'PY'
import json
import sys

value = {
    "schema_version": "rethlas_review_adapter_command_v1",
    "command": "review_status",
    "payload": {
        "operation": "memory_batch_publication_status",
        "problem_id": sys.argv[1],
    },
}
print(json.dumps(value, allow_nan=False, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
PY
  )" || return 70
  if ! response="$(run_owner_control review-status "$envelope")"; then
    echo "Could not read the owner-authenticated memory publication manifest." >&2
    return 70
  fi
  if ! canonical="$(
    RETHLAS_OWNER_MEMORY_BATCH_STATUS_RAW_JSON="$response" \
      "$TRUSTED_PYTHON_BIN" -I -B - <<'PY'
import json
import os

MAX_BYTES = 262_144


def reject_duplicates(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def reject_constant(value):
    raise ValueError(f"non-finite JSON constant: {value}")


raw = os.environ["RETHLAS_OWNER_MEMORY_BATCH_STATUS_RAW_JSON"]
encoded = raw.encode("utf-8")
if not encoded or len(encoded) > MAX_BYTES:
    raise SystemExit("owner memory publication manifest exceeds its byte bound")
value = json.loads(
    raw,
    object_pairs_hook=reject_duplicates,
    parse_constant=reject_constant,
)
canonical = json.dumps(
    value,
    allow_nan=False,
    ensure_ascii=False,
    sort_keys=True,
    separators=(",", ":"),
)
if len(canonical.encode("utf-8")) > MAX_BYTES:
    raise SystemExit("canonical owner memory publication manifest exceeds its byte bound")
print(canonical)
PY
  )"; then
    echo "Owner memory publication manifest was not bounded strict JSON." >&2
    return 70
  fi
  printf '%s' "$canonical"
}

generation_control_receipt() {
  local receipt owner_manifest_snapshot
  if [[ "$REVIEW_CADENCE_POLICY" != disabled ]]; then
    owner_manifest_snapshot="$(
      owner_memory_batch_publication_snapshot
    )" || return 70
    if ! receipt="$(
      RETHLAS_OWNER_MEMORY_BATCH_PUBLICATION_SNAPSHOT_JSON="$owner_manifest_snapshot" \
        "$TRUSTED_PYTHON_BIN" "${TRUSTED_MCP_LOADER_ARGS[@]}" -- \
          --generation-control-receipt "$problem_rel"
    )"; then
      echo "Could not read the trusted generation-control receipt." >&2
      return 70
    fi
  elif ! receipt="$(
    "$TRUSTED_PYTHON_BIN" "${TRUSTED_MCP_LOADER_ARGS[@]}" -- \
      --generation-control-receipt "$problem_rel"
  )"; then
    echo "Could not read the trusted generation-control receipt." >&2
    return 70
  fi
  RETHLAS_GENERATION_CONTROL_RECEIPT_JSON="$receipt" \
    "$TRUSTED_PYTHON_BIN" -I -B - \
      "$problem_rel" "$RETHLAS_EXPECTED_STATEMENT_SHA256" \
      "$RETHLAS_GENERATION_CONTROL_TOKEN" <<'PY'
import hashlib
import json
import os
import re
import sys


def fail(message: str) -> None:
    print(f"invalid generation-control receipt: {message}", file=sys.stderr)
    raise SystemExit(70)


try:
    receipt = json.loads(os.environ["RETHLAS_GENERATION_CONTROL_RECEIPT_JSON"])
except (json.JSONDecodeError, UnicodeError) as exc:
    fail(f"not strict JSON: {exc}")
if not isinstance(receipt, dict) or set(receipt) != {
    "schema_version",
    "control",
    "record_sha256",
}:
    fail("top-level fields mismatch")
if receipt["schema_version"] != "rethlas_generation_control_receipt_v1":
    fail("schema version mismatch")
control = receipt["control"]
if not isinstance(control, dict) or set(control) != {
    "schema",
    "instance_id",
    "problem_id",
    "statement_sha256",
    "state",
    "reason",
    "evidence_record_ids",
}:
    fail("control fields mismatch")
if (
    control["schema"] != "rethlas_generation_control_v1"
    or control["instance_id"] != sys.argv[3]
    or re.fullmatch(r"[0-9a-f]{32}", control["instance_id"] or "") is None
    or control["problem_id"] != sys.argv[1]
    or control["statement_sha256"] != sys.argv[2]
    or re.fullmatch(r"[0-9a-f]{64}", control["statement_sha256"] or "") is None
):
    fail("control bindings mismatch")
state = control["state"]
if state not in {
    "running",
    "waiting_cost_gate",
    "waiting_owner_advisor_decision",
}:
    fail("control state mismatch")
reason = control["reason"]
if (
    not isinstance(reason, str)
    or not reason
    or reason != reason.strip()
    or "\x00" in reason
    or len(reason.encode("utf-8")) > 4096
):
    fail("control reason is invalid")
evidence = control["evidence_record_ids"]
if not isinstance(evidence, list) or any(
    not isinstance(record_id, str)
    or re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", record_id) is None
    for record_id in evidence
):
    fail("control evidence ids are invalid")
if len(set(evidence)) != len(evidence):
    fail("control evidence ids are duplicated")
if state == "running" and reason != "owner_runner_started":
    fail("running control reason is not owner_runner_started")
if (state == "running" and evidence) or (
    state != "running" and not 1 <= len(evidence) <= 16
):
    fail("control evidence does not match its state")
encoded = json.dumps(
    control,
    allow_nan=False,
    ensure_ascii=False,
    sort_keys=True,
    separators=(",", ":"),
).encode("utf-8")
actual = hashlib.sha256(encoded).hexdigest()
if receipt["record_sha256"] != actual:
    fail("record SHA-256 mismatch")
print(json.dumps(
    receipt,
    allow_nan=False,
    ensure_ascii=False,
    sort_keys=True,
    separators=(",", ":"),
))
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
  else
    echo "Ignoring untrusted/stale verified blueprint at $verified_path"
  fi
fi

prepare_references

HOTJOIN_ADAPTER_SHA256=""
ADVISOR_BRIDGE_SHA256=""
GUARDIAN_SOURCE_SHA256=""
GUARDIAN_LAUNCHER_SHA256=""
GUARDIAN_RUNNER_SHA256=""
POLICY_CONTRACT_SHA256=""
REVIEW_POLICY_SHA256=""
CONTROL_CAPABILITY_REVISION=""
if [[ -n "$RETHLAS_HOTJOIN_RUN_ID" ]]; then
  if [[ ! -f "$HOTJOIN_ADAPTER" || -L "$HOTJOIN_ADAPTER" ]]; then
    echo "Hot-join adapter must be a regular non-symlink file: $HOTJOIN_ADAPTER" >&2
    exit 1
  fi
  HOTJOIN_ADAPTER_SHA256="$(
    "$TRUSTED_PYTHON_BIN" -I -B -c \
      'import hashlib, pathlib, sys; print(hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest())' \
      "$HOTJOIN_ADAPTER"
  )"
  for control_source in "$GUARDIAN_SOURCE" "$GUARDIAN_LAUNCHER" "$GUARDIAN_RUNNER"; do
    if [[ ! -f "$control_source" || -L "$control_source" ]]; then
      echo "Guardian control source must be a regular non-symlink file: $control_source" >&2
      exit 1
    fi
  done
  GUARDIAN_SOURCE_SHA256="$(
    "$TRUSTED_PYTHON_BIN" -I -B -c \
      'import hashlib, pathlib, sys; print(hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest())' \
      "$GUARDIAN_SOURCE"
  )"
  GUARDIAN_LAUNCHER_SHA256="$(
    "$TRUSTED_PYTHON_BIN" -I -B -c \
      'import hashlib, pathlib, sys; print(hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest())' \
      "$GUARDIAN_LAUNCHER"
  )"
  GUARDIAN_RUNNER_SHA256="$(
    "$TRUSTED_PYTHON_BIN" -I -B -c \
      'import hashlib, pathlib, sys; print(hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest())' \
      "$GUARDIAN_RUNNER"
  )"
  if [[ ! -f "$ADVISOR_BRIDGE" || -L "$ADVISOR_BRIDGE" ]]; then
    echo "Advisor bridge must be a regular non-symlink file: $ADVISOR_BRIDGE" >&2
    exit 1
  fi
  ADVISOR_BRIDGE_SHA256="$(
    "$TRUSTED_PYTHON_BIN" -I -B -c \
      'import hashlib, pathlib, sys; print(hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest())' \
      "$ADVISOR_BRIDGE"
  )"
  if [[ -n "${RETHLAS_ADVISOR_RECEIPTS_ROOT:-}" \
     && "$RETHLAS_ADVISOR_RECEIPTS_ROOT" != "$ADVISOR_RECEIPTS_ROOT" ]]; then
    echo "Advisor receipt root is fixed outside the generation workspace and cannot be overridden." >&2
    exit 1
  fi
  if [[ -L "$ADVISOR_ROOT" || -L "$ADVISOR_RECEIPTS_ROOT" ]]; then
    echo "Advisor state and receipt roots must not be symlinks." >&2
    exit 1
  fi
  umask 077
  mkdir -p "$ADVISOR_RECEIPTS_ROOT"
  if ! "$TRUSTED_PYTHON_BIN" -I -B - "$ADVISOR_ROOT" "$ADVISOR_RECEIPTS_ROOT" <<'PY'
import os
import stat
import sys
from pathlib import Path

for raw in sys.argv[1:]:
    path = Path(raw).absolute()
    metadata = path.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or path.is_symlink():
        raise SystemExit(1)
    if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
        raise SystemExit(1)
    if stat.S_IMODE(metadata.st_mode) & 0o077:
        raise SystemExit(1)
PY
  then
    echo "Advisor state and receipt roots must be owner-only real directories." >&2
    exit 1
  fi
  export RETHLAS_ADVISOR_RECEIPTS_ROOT="$ADVISOR_RECEIPTS_ROOT"
  export RETHLAS_EXPECTED_HOTJOIN_RUN_ID="$RETHLAS_HOTJOIN_RUN_ID"
  if [[ "$REVIEW_CADENCE_POLICY" != disabled ]]; then
    export RETHLAS_REVIEW_ADAPTER_PATH="$HOTJOIN_ADAPTER"
    export RETHLAS_REVIEW_ADAPTER_SHA256="$HOTJOIN_ADAPTER_SHA256"
    export RETHLAS_REVIEW_DB="$HOTJOIN_DB"
    if ! policy_contract_json="$(
      "$TRUSTED_PYTHON_BIN" -I -B "$HOTJOIN_ADAPTER" policy-contract
    )"; then
      echo "Hot-join adapter policy-contract preflight failed; refusing to start Codex." >&2
      exit 70
    fi
    if ! POLICY_CONTRACT_SHA256="$(
      RETHLAS_POLICY_CONTRACT_JSON="$policy_contract_json" \
        "$TRUSTED_PYTHON_BIN" -I -B - \
          "$REVIEW_CADENCE_POLICY" "$CONTEXT_GUARD_POLICY" \
          "$GUARDIAN_SOURCE_SHA256" "$GUARDIAN_LAUNCHER_SHA256" \
          "$GUARDIAN_RUNNER_SHA256" <<'PY'
import hashlib
import json
import os
import re
import sys


def fail(message: str) -> None:
    print(f"invalid hot-join policy contract: {message}", file=sys.stderr)
    raise SystemExit(1)


try:
    value = json.loads(os.environ["RETHLAS_POLICY_CONTRACT_JSON"])
except (json.JSONDecodeError, UnicodeError) as exc:
    fail(f"not strict JSON: {exc}")
if not isinstance(value, dict) or set(value) != {
    "schema_version",
    "review_cadence_policy",
    "continuous_supervisor_policy",
    "context_guard_policy",
    "contract_sha256",
}:
    fail("top-level fields do not match rethlas-policy-contract-v1")
if value["schema_version"] != "rethlas-policy-contract-v1":
    fail("schema_version mismatch")
review = value["review_cadence_policy"]
context = value["context_guard_policy"]
if sys.argv[1] == "rethlas_continuous_supervisor_v1":
    review = value["continuous_supervisor_policy"]
else:
    review = value["review_cadence_policy"]
if not isinstance(review, dict) or review.get("policy_id") != sys.argv[1]:
    fail("review cadence policy id mismatch")
if not isinstance(context, dict) or context.get("policy_id") != sys.argv[2]:
    fail("context guard policy id mismatch")
try:
    cost_manifest = json.loads(os.environ["RETHLAS_RESOLVED_COST_POLICY_JSON"])
except (KeyError, json.JSONDecodeError, UnicodeError) as exc:
    fail(f"resolved cost policy manifest is unavailable: {exc}")
if not isinstance(cost_manifest, dict) or set(cost_manifest) != {
    "schema_version", "policy", "authority"
}:
    fail("resolved cost policy manifest shape mismatch")
cost_manifest_json = json.dumps(
    cost_manifest,
    allow_nan=False,
    ensure_ascii=False,
    sort_keys=True,
    separators=(",", ":"),
)
cost_manifest_sha256 = hashlib.sha256(cost_manifest_json.encode("utf-8")).hexdigest()
if (
    cost_manifest.get("schema_version") != "rethlas_resolved_cost_policy_v1"
    or cost_manifest.get("policy") not in {"owner_gated", "disabled_by_owner"}
    or cost_manifest.get("authority") != "owner_wrapper"
    or os.environ.get("RETHLAS_RESOLVED_COST_POLICY_SHA256")
    != cost_manifest_sha256
    or review.get("owner_cost_gate_policy") != cost_manifest["policy"]
    or review.get("owner_cost_gate_policy_manifest_schema")
    != cost_manifest["schema_version"]
    or review.get("owner_cost_gate_policy_manifest_sha256")
    != cost_manifest_sha256
):
    fail("resolved cost policy differs from the selected review policy")

# The runner verifies the safety-critical constants independently. The full
# object is then content-addressed, so additional descriptive fields cannot
# drift silently between wrapper and adapter.
if sys.argv[1] == "rethlas_continuous_supervisor_v1":
    expected = {
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
        "guardian_mode": "monitor_only",
    }
    if any(review.get(key) != item for key, item in expected.items()):
        fail("continuous-supervisor constants mismatch")
    if (
        review.get("state_machine_schema")
        != "rethlas_continuous_state_machine_v1"
        or re.fullmatch(r"[0-9a-f]{64}", review.get("state_machine_sha256", ""))
        is None
        or re.fullmatch(r"[0-9a-f]{64}", review.get("resource_policy_sha256", ""))
        is None
    ):
        fail("continuous-supervisor state machine commitment mismatch")
else:
    if review.get("review_1_due_seconds") != 3600:
        fail("first review must be due at exactly T+60m")
    if review.get("review_1_deadline_seconds") != 4200:
        fail("first reviewer deadline must be exactly T+70m")
    if review.get("review_2_due_seconds") != 7200:
        fail("second review must be due at exactly T+120m")
    if review.get("review_2_deadline_seconds") != 7800:
        fail("second reviewer deadline must be exactly T+130m")
    if review.get("close_notice_due_seconds") != 8820:
        fail("close offset must be exactly T+147m")
    if review.get("hard_stop_due_seconds") != 9000 or review.get("cycle_seconds") != 9000:
        fail("hard stop must be exactly T+150m")
    if review.get("review_boundary_mode") != "cooperative_drain_then_deadline_interrupt":
        fail("review boundaries must use cooperative drain before forced interruption")
    if review.get("review_drain_grace_seconds") != 300:
        fail("review drain grace must be exactly five minutes")
    if review.get("review_execution_grace_seconds") != 300:
        fail("review execution grace must be exactly five minutes")
    if review.get("two_yellow_without_progress_is_red") is not True:
        fail("two-yellow no-progress rule is not enabled")
    if review.get("hard_stop_interrupt_is_expected") is not True:
        fail("hard-stop interrupt rule is not enabled")
guardian_ready = review.get("guardian_enforcement_ready")
if type(guardian_ready) is not bool:
    fail("guardian_enforcement_ready must be an immutable boolean")
if guardian_ready is not True:
    fail(
        "guardian enforcement is not released; zero paid, recovery, reviewer, "
        "or root work is allowed"
    )
if review.get("max_concurrent_proof_lanes") != 3:
    fail("review policy must enforce exactly three concurrent proof lanes")
if review.get("clock") not in {
    "earliest_durable_wall_and_same_boot_monotonic",
    "global_rolling_wall_and_same_boot_monotonic",
}:
    fail("cadence clock is not an approved durable dual clock")
if review.get("approved_guardian_sha256") != sys.argv[3]:
    fail("Guardian source differs from its released policy pin")
if review.get("approved_guardian_launcher_sha256") != sys.argv[4]:
    fail("Guardian launcher differs from its released policy pin")
if review.get("approved_guardian_runner_sha256") != sys.argv[5]:
    fail("Guardian runner differs from its released policy pin")
if sys.argv[1] != "rethlas_continuous_supervisor_v1":
    if review.get("review_is_independent") is not True or review.get("review_is_not_fact_check") is not True:
        fail("independent route-review semantics mismatch")
    if review.get("review_verdicts") != ["green", "yellow", "red"]:
        fail("review verdict set mismatch")

expected_thresholds = {
    "observe": {"ratio_gte": 0.60, "headroom_lte": 112000},
    "checkpoint_required": {"ratio_gte": 0.65, "headroom_lte": 96000},
    "fresh_thread_required": {"ratio_gte": 0.70, "headroom_lte": 80000},
    "emergency": {"ratio_gte": 0.82, "headroom_lte": 48000},
}
if any(context.get(name) != threshold for name, threshold in expected_thresholds.items()):
    fail("context thresholds mismatch")
if context.get("occupancy_numerator") != "last.inputTokens" or context.get("occupancy_denominator") != "modelContextWindow":
    fail("context occupancy formula mismatch")
if context.get("cached_input_tokens_reduce_occupancy") is not False:
    fail("cached-token occupancy rule mismatch")
if context.get("max_handoff_utf8_bytes") != 32768:
    fail("handoff bound mismatch")
if context.get("compaction_forces_fresh_thread") is not True:
    fail("compaction fresh-thread rule mismatch")
if context.get("fresh_thread_must_not_resume_or_fork") is not True:
    fail("fresh-thread requirement mismatch")

for label, policy in (("review", review), ("context", context)):
    policy_sha = policy.get("policy_sha256")
    if not isinstance(policy_sha, str) or re.fullmatch(r"[0-9a-f]{64}", policy_sha) is None:
        fail(f"{label} policy_sha256 is invalid")
    policy_material = dict(policy)
    del policy_material["policy_sha256"]
    policy_actual = hashlib.sha256(json.dumps(
        policy_material,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    if policy_actual != policy_sha:
        fail(f"{label} policy_sha256 mismatch")

claimed = value["contract_sha256"]
if not isinstance(claimed, str) or re.fullmatch(r"[0-9a-f]{64}", claimed) is None:
    fail("contract_sha256 is not lowercase SHA-256")
material = dict(value)
del material["contract_sha256"]
encoded = json.dumps(
    material,
    allow_nan=False,
    ensure_ascii=False,
    sort_keys=True,
    separators=(",", ":"),
).encode("utf-8")
actual = hashlib.sha256(encoded).hexdigest()
if actual != claimed:
    fail("contract_sha256 mismatch")
print(actual)
PY
    )"; then
      echo "Hot-join cadence policy contract is incompatible; refusing to start Codex." >&2
      exit 70
    fi
    export RETHLAS_REVIEW_CADENCE_POLICY="$REVIEW_CADENCE_POLICY"
    export RETHLAS_CONTEXT_GUARD_POLICY="$CONTEXT_GUARD_POLICY"
    export RETHLAS_POLICY_CONTRACT_SHA256="$POLICY_CONTRACT_SHA256"
    REVIEW_POLICY_SHA256="$(
      RETHLAS_POLICY_CONTRACT_JSON="$policy_contract_json" \
      RETHLAS_SELECTED_REVIEW_POLICY="$REVIEW_CADENCE_POLICY" \
        "$TRUSTED_PYTHON_BIN" -I -B -c \
          'import json, os; value=json.loads(os.environ["RETHLAS_POLICY_CONTRACT_JSON"]); key="continuous_supervisor_policy" if os.environ["RETHLAS_SELECTED_REVIEW_POLICY"] == "rethlas_continuous_supervisor_v1" else "review_cadence_policy"; print(value[key]["policy_sha256"])'
    )"
    export RETHLAS_REVIEW_EXPECTED_MODEL="$MODEL"
    export RETHLAS_REVIEW_EXPECTED_REASONING_EFFORT="$REASONING_EFFORT"
    export RETHLAS_REVIEW_POLICY_SHA256="$REVIEW_POLICY_SHA256"
    current_hotjoin_sha256="$(
      "$TRUSTED_PYTHON_BIN" -I -B -c \
        'import hashlib, pathlib, sys; print(hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest())' \
        "$HOTJOIN_ADAPTER"
    )"
    if [[ "$current_hotjoin_sha256" != "$HOTJOIN_ADAPTER_SHA256" ]] \
       || ! trusted_runtime_unchanged; then
      echo "Trusted cadence/review sources changed during policy preflight; refusing to initialize run state." >&2
      exit 70
    fi
    if ! "$TRUSTED_PYTHON_BIN" -I -B "$HOTJOIN_ADAPTER" \
      --db "$HOTJOIN_DB" init \
      --run-id "$RETHLAS_HOTJOIN_RUN_ID" --problem-id "$problem_rel" >/dev/null; then
      echo "Could not initialize durable cadence state; refusing to start Codex." >&2
      exit 70
    fi
  else
    unset RETHLAS_REVIEW_ADAPTER_PATH
    unset RETHLAS_REVIEW_ADAPTER_SHA256
    unset RETHLAS_REVIEW_DB
    unset RETHLAS_REVIEW_EXPECTED_MODEL
    unset RETHLAS_REVIEW_EXPECTED_REASONING_EFFORT
    unset RETHLAS_REVIEW_POLICY_SHA256
  fi
else
  # Legacy runs have no owner-authorized advisor delivery surface.  Never let
  # inherited values silently enable the receipt reader or bind it to a stale
  # run id.
  unset RETHLAS_ADVISOR_RECEIPTS_ROOT
  unset RETHLAS_EXPECTED_HOTJOIN_RUN_ID
  unset RETHLAS_REVIEW_CADENCE_POLICY
  unset RETHLAS_CONTEXT_GUARD_POLICY
  unset RETHLAS_POLICY_CONTRACT_SHA256
  unset RETHLAS_REVIEW_ADAPTER_PATH
  unset RETHLAS_REVIEW_ADAPTER_SHA256
  unset RETHLAS_REVIEW_DB
  unset RETHLAS_REVIEW_EXPECTED_MODEL
  unset RETHLAS_REVIEW_EXPECTED_REASONING_EFFORT
  unset RETHLAS_REVIEW_POLICY_SHA256
fi

# Codex capability/version discovery comes only after every local policy,
# runtime, helper, database, and cadence preflight above. A rejected scheduler
# contract therefore launches no Codex process at all.
codex_command="$(command -v codex || true)"
if [[ "$codex_command" != /* ]] || [[ ! -x "$codex_command" ]]; then
  echo "codex must resolve to an absolute executable path." >&2
  exit 1
fi
attest_codex_binary() {
  "$TRUSTED_PYTHON_BIN" -I -B - "$1" <<'PY'
import hashlib
import json
import os
import stat
import sys
from pathlib import Path


def fail(message: str) -> None:
    print(f"unsafe Codex executable: {message}", file=sys.stderr)
    raise SystemExit(1)


source = Path(os.path.abspath(sys.argv[1]))
try:
    target = source.resolve(strict=True)
    before = target.lstat()
except (OSError, RuntimeError) as exc:
    fail(f"cannot resolve executable: {exc}")
if target.is_symlink() or not stat.S_ISREG(before.st_mode):
    fail("resolved target must be a regular non-symlink file")
if stat.S_IMODE(before.st_mode) & 0o022:
    fail("resolved target must not be group/world-writable")
allowed_uids = {0}
if hasattr(os, "geteuid"):
    allowed_uids.add(os.geteuid())
if hasattr(before, "st_uid") and before.st_uid not in allowed_uids:
    fail("resolved target must be owned by the current owner or root")
if not os.access(target, os.X_OK):
    fail("resolved target is not executable")
flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
descriptor = os.open(target, flags)
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
    while chunk := os.read(descriptor, 65536):
        digest.update(chunk)
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
hotjoin_control_sources_unchanged() {
  local current_hotjoin current_advisor current_guardian current_launcher current_runner
  current_hotjoin="$(
    "$TRUSTED_PYTHON_BIN" -I -B -c \
      'import hashlib, pathlib, sys; print(hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest())' \
      "$HOTJOIN_ADAPTER"
  )" || return 1
  current_advisor="$(
    "$TRUSTED_PYTHON_BIN" -I -B -c \
      'import hashlib, pathlib, sys; print(hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest())' \
      "$ADVISOR_BRIDGE"
  )" || return 1
  current_guardian="$(
    "$TRUSTED_PYTHON_BIN" -I -B -c \
      'import hashlib, pathlib, sys; print(hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest())' \
      "$GUARDIAN_SOURCE"
  )" || return 1
  current_launcher="$(
    "$TRUSTED_PYTHON_BIN" -I -B -c \
      'import hashlib, pathlib, sys; print(hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest())' \
      "$GUARDIAN_LAUNCHER"
  )" || return 1
  current_runner="$(
    "$TRUSTED_PYTHON_BIN" -I -B -c \
      'import hashlib, pathlib, sys; print(hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest())' \
      "$GUARDIAN_RUNNER"
  )" || return 1
  [[ "$current_hotjoin" == "$HOTJOIN_ADAPTER_SHA256" ]] \
    && [[ "$current_advisor" == "$ADVISOR_BRIDGE_SHA256" ]] \
    && [[ "$current_guardian" == "$GUARDIAN_SOURCE_SHA256" ]] \
    && [[ "$current_launcher" == "$GUARDIAN_LAUNCHER_SHA256" ]] \
    && [[ "$current_runner" == "$GUARDIAN_RUNNER_SHA256" ]] \
    && trusted_runtime_unchanged \
    && codex_executable_unchanged
}
CODEX_VERSION="$("$CODEX_BIN" --version 2>/dev/null || echo 'unknown')"
echo "========================================"
echo " Codex:      $CODEX_VERSION"
echo " Model:      $MODEL"
echo " Effort:     $REASONING_EFFORT"
echo " Mode:       $PUBLIC_RUN_MODE"
echo " Problem:    $PROBLEM_FILE"
echo " Problem ID: $problem_rel"
echo " References: $ref_dir"
echo " Math Python: $trusted_python_command"
echo " Max iters:  $MAX_ITERATIONS"
if [[ "$REVIEW_CADENCE_POLICY" == rethlas_continuous_supervisor_v1 ]]; then
  echo " Supervisor:  rolling 60-minute reviews; 150-minute renewal is nonterminal"
elif [[ "$REVIEW_CADENCE_POLICY" == rethlas_route_review_150m_v2 ]]; then
  echo " Construction: $DEEP_WORK_MINUTES minutes (bounded T0-T+60m interval)"
else
  echo " Deep work:  $DEEP_WORK_MINUTES minutes (soft target)"
fi
echo " Logs:       $ACTIVE_LOG_DIR"
echo " Stop file:  $verified_path"
if [[ -n "$RETHLAS_HOTJOIN_RUN_ID" ]]; then
  echo " Hot join:   $RETHLAS_HOTJOIN_RUN_ID"
  echo " Join DB:    $HOTJOIN_DB"
  echo " Cadence:    $REVIEW_CADENCE_POLICY"
  echo " Context:    $CONTEXT_GUARD_POLICY"
else
  echo " Hot join:   disabled (legacy fresh-session loop)"
  echo " Cadence:    disabled"
fi
echo "========================================"
echo ""

VERIFY_HEALTH_URL="${VERIFY_HEALTH_URL:-${VERIFY_URL:-http://127.0.0.1:8091/health}}"
verify_base_url="${VERIFY_HEALTH_URL%/health}"
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
if ! curl -sf "$VERIFY_HEALTH_URL" >/dev/null 2>&1; then
  echo "WARNING: verification service not reachable at ${VERIFY_HEALTH_URL}"
  echo "         The agent may be unable to produce blueprint_verified.md."
  echo "         Start it first if you need verified proofs."
  echo ""
fi
TRUSTED_MCP_ENV_TOML="$("$TRUSTED_PYTHON_BIN" -I -B - <<'PY'
import json
import os

names = (
    "PYTHONDONTWRITEBYTECODE",
    "RETHLAS_GENERATION_CONTROL_TOKEN",
    "RETHLAS_EXPECTED_PROBLEM_ID",
    "RETHLAS_EXPECTED_HOTJOIN_RUN_ID",
    "RETHLAS_EXPECTED_STATEMENT_SHA256",
    "RETHLAS_GENERATION_ROOT",
    "RETHLAS_ADVISOR_RECEIPTS_ROOT",
    "RETHLAS_REVIEW_CADENCE_POLICY",
    "RETHLAS_CONTEXT_GUARD_POLICY",
    "RETHLAS_POLICY_CONTRACT_SHA256",
    "RETHLAS_REVIEW_CONTRACT_CLI_PATH",
    "RETHLAS_REVIEW_CONTRACT_CLI_SHA256",
    "RETHLAS_REVIEW_ADAPTER_PATH",
    "RETHLAS_REVIEW_ADAPTER_SHA256",
    "RETHLAS_REVIEW_DB",
    "RETHLAS_REVIEW_EXPECTED_MODEL",
    "RETHLAS_REVIEW_EXPECTED_REASONING_EFFORT",
    "RETHLAS_REVIEW_POLICY_SHA256",
    "RETHLAS_RECEIPTS_ROOT",
    "RETHLAS_RESOLVED_COST_POLICY_JSON",
    "RETHLAS_RESOLVED_COST_POLICY_SHA256",
    "RETHLAS_TRUSTED_RUNTIME_SHA256",
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

validate_cadence_projection() {
  local projection="$1"
  RETHLAS_CADENCE_PROJECTION_JSON="$projection" \
  RETHLAS_POLICY_CONTRACT_JSON="$policy_contract_json" \
    "$TRUSTED_PYTHON_BIN" -I -B - \
      "$RETHLAS_HOTJOIN_RUN_ID" "$REVIEW_CADENCE_POLICY" \
      "$CONTEXT_GUARD_POLICY" <<'PY'
import json
import os
import re
import sys
import time


def fail(message: str) -> None:
    print(f"invalid cadence-control-state projection: {message}", file=sys.stderr)
    raise SystemExit(70)


try:
    value = json.loads(os.environ["RETHLAS_CADENCE_PROJECTION_JSON"])
except (json.JSONDecodeError, UnicodeError) as exc:
    fail(f"not strict JSON: {exc}")
expected_fields = {
    "run_id",
    "disposition",
    "paid_turn_allowed",
    "review_cadence",
    "continuous_supervisor",
    "context_guard",
    "thread_epoch",
    "quarantine",
}
if not isinstance(value, dict) or set(value) != expected_fields:
    fail("top-level fields mismatch")
if value["run_id"] != sys.argv[1]:
    fail("run id mismatch")
disposition = value["disposition"]
if not isinstance(disposition, str) or re.fullmatch(r"[a-z][a-z0-9_]{0,63}", disposition) is None:
    fail("disposition is invalid")
if not isinstance(value["paid_turn_allowed"], bool):
    fail("paid_turn_allowed must be boolean")
review = value["review_cadence"]
context = value["context_guard"]
if not isinstance(review, dict) or review.get("policy_id") != sys.argv[2]:
    fail("review cadence projection mismatch")
if not isinstance(context, dict) or context.get("policy_id") != sys.argv[3]:
    fail("context guard projection mismatch")
if not isinstance(context.get("adapter_resume_allowed"), bool):
    fail("context guard adapter_resume_allowed must be boolean")
if not isinstance(context.get("operational_failures"), list):
    fail("context guard operational_failures must be an array")
continuous = value["continuous_supervisor"]
if sys.argv[2] == "rethlas_continuous_supervisor_v1":
    if continuous is not None:
        required_continuous = {
            "policy_id",
            "policy_sha256",
            "state",
            "origin_wall_epoch",
            "next_review_ordinal",
            "next_review_wall_epoch",
            "next_renewal_ordinal",
            "next_renewal_wall_epoch",
            "logical_root_epoch",
            "total_paused_wall_seconds",
            "current_review",
            "latest_renewal",
        }
        if (
            not isinstance(continuous, dict)
            or set(continuous) != required_continuous
            or continuous["policy_id"] != sys.argv[2]
            or continuous["state"] not in {
                "active",
                "review_collecting",
                "review_running",
                "review_applying",
                "candidate_verification",
                "advisor_checkpoint_required",
                "waiting_owner_advisor_decision",
                "paused_owner",
                "operational_blocked",
                "completed",
            }
            or not isinstance(continuous["logical_root_epoch"], int)
            or isinstance(continuous["logical_root_epoch"], bool)
            or continuous["logical_root_epoch"] < 1
        ):
            fail("continuous supervisor projection is malformed")
elif continuous is not None:
    fail("bounded cadence unexpectedly exposes a continuous supervisor")
try:
    contract = json.loads(os.environ["RETHLAS_POLICY_CONTRACT_JSON"])
except (json.JSONDecodeError, UnicodeError) as exc:
    fail(f"cannot re-read authenticated policy contract: {exc}")
review_contract_key = (
    "continuous_supervisor_policy"
    if sys.argv[2] == "rethlas_continuous_supervisor_v1"
    else "review_cadence_policy"
)
if review.get("policy_digest") != contract[review_contract_key]["policy_sha256"]:
    fail("review cadence policy digest mismatch")
if context.get("policy_digest") != contract["context_guard_policy"]["policy_sha256"]:
    fail("context guard policy digest mismatch")
if "continuation" not in review or "review_boundary" not in review:
    fail("review cadence omitted continuation or boundary projection")
continuation = review["continuation"]
if continuation is not None:
    continuation_fields = {
        "authorization_id",
        "expires_at",
        "mode",
        "reserved",
        "review_action_id",
        "state",
        "superseded",
    }
    if not isinstance(continuation, dict) or set(continuation) != continuation_fields:
        fail("continuation projection fields mismatch")
    if (
        not isinstance(continuation["authorization_id"], str)
        or re.fullmatch(r"cadauth_[0-9a-f]{32}", continuation["authorization_id"])
        is None
        or not isinstance(continuation["expires_at"], (int, float))
        or isinstance(continuation["expires_at"], bool)
        or continuation["mode"] not in {"active_cycle", "review_only"}
        or not isinstance(continuation["reserved"], bool)
        or not isinstance(continuation["superseded"], bool)
        or continuation["state"]
        not in {"prepared", "consumed", "execution_unknown"}
        or (
            continuation["mode"] == "active_cycle"
            and continuation["review_action_id"] is not None
        )
        or (
            continuation["mode"] == "review_only"
            and (
                not isinstance(continuation["review_action_id"], str)
                or not continuation["review_action_id"]
            )
        )
    ):
        fail("continuation projection values mismatch")
boundary = review["review_boundary"]
if boundary is not None:
    boundary_fields = {
        "boundary_id",
        "no_live_descendants_sha256",
        "review_ordinal",
        "root_terminal_sha256",
        "root_thread_id",
        "root_turn_id",
        "state",
    }
    digest_or_none = lambda item: item is None or (
        isinstance(item, str) and re.fullmatch(r"[0-9a-f]{64}", item) is not None
    )
    if (
        not isinstance(boundary, dict)
        or set(boundary) != boundary_fields
        or re.fullmatch(r"reviewbound_[0-9a-f]{32}", boundary.get("boundary_id", ""))
        is None
        or boundary.get("review_ordinal") not in {1, 2}
        or not isinstance(boundary.get("root_thread_id"), str)
        or not boundary["root_thread_id"]
        or not isinstance(boundary.get("root_turn_id"), str)
        or not boundary["root_turn_id"]
        or boundary.get("state")
        not in {
            "root_dispatching",
            "root_accepted",
            "root_terminal",
            "descendants_terminal",
            "execution_unknown",
            "operational_blocked",
        }
        or not digest_or_none(boundary.get("root_terminal_sha256"))
        or not digest_or_none(boundary.get("no_live_descendants_sha256"))
        or (
            boundary["state"] == "descendants_terminal"
            and (
                boundary["root_terminal_sha256"] is None
                or boundary["no_live_descendants_sha256"] is None
            )
        )
        or (
            boundary["state"] in {"root_terminal", "descendants_terminal"}
            and boundary["root_terminal_sha256"] is None
        )
        or (
            boundary["state"] != "descendants_terminal"
            and boundary["no_live_descendants_sha256"] is not None
        )
    ):
        fail("review boundary projection values mismatch")
required_mode = {
    "continue_active_cycle": "active_cycle",
}.get(disposition)
if disposition == "continuation_authorization_required" and review.get(
    "allowed_action"
) not in {
    "free_construction",
    "continue_to_next_milestone",
    "one_bounded_cycle_on_fatal_doubt",
}:
    fail("ordinary active-cycle continuation conflicts with the durable allowed action")
if (
    disposition == "continue_active_cycle"
    and review.get("allowed_action") != "continue_active_cycle_authorized"
):
    fail("authorized active-cycle continuation lacks its durable allowed action")
if (
    disposition == "continue_reviewed_cycle_fresh_epoch"
    and review.get("allowed_action") != "post_review_handoff_required"
):
    fail("reviewed fresh-epoch continuation lacks its durable handoff action")
if disposition == "route_frozen" and review.get("allowed_action") not in {
    "freeze_route",
    "recovery_only",
}:
    fail("frozen route projection lacks terminal freeze authority")
if required_mode is not None and (
    continuation is None
    or continuation["mode"] != required_mode
    or continuation["state"] != "prepared"
    or continuation["reserved"] is not False
    or continuation["superseded"] is not False
    or float(continuation["expires_at"]) <= time.time()
):
    fail("paid continuation is absent, stale, reserved, or mode-mismatched")
print(json.dumps(value, allow_nan=False, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
PY
}

cadence_control_projection() {
  local projection
  if ! projection="$(
    "$TRUSTED_PYTHON_BIN" -I -B "$HOTJOIN_ADAPTER" \
      --db "$HOTJOIN_DB" cadence-control-state \
      --run-id "$RETHLAS_HOTJOIN_RUN_ID"
  )"; then
    echo "Could not read durable cadence disposition." >&2
    return 70
  fi
  validate_cadence_projection "$projection"
}

cadence_projection_disposition() {
  local projection="$1"
  RETHLAS_CADENCE_PROJECTION_JSON="$projection" \
    "$TRUSTED_PYTHON_BIN" -I -B -c \
      'import json, os; print(json.loads(os.environ["RETHLAS_CADENCE_PROJECTION_JSON"])["disposition"])'
}

# Privileged owner controls use one-shot FIFO capabilities after Guardian
# release.  Frame the in-memory token and canonical JSON on this wrapper's
# stdin, then execute the already-attested adapter bytes with the token on an
# inherited pipe.  The raw capability never enters argv, env, the ledger, or a
# log file.
OWNER_CONTROL_BOOTSTRAP=''
read -r -d '' OWNER_CONTROL_BOOTSTRAP <<'PY' || true
import hashlib
import io
import os
import re
import stat
import sys

MAX_INPUT = 2 * 1024 * 1024
raw = sys.stdin.buffer.read(MAX_INPUT + 1)
if len(raw) > MAX_INPUT or b"\n" not in raw:
    raise SystemExit("owner control frame is malformed")
token, envelope = raw.split(b"\n", 1)
if re.fullmatch(rb"[0-9a-f]{64}", token) is None or not envelope:
    raise SystemExit("owner control frame is invalid")
path, expected, database, command = sys.argv[1:5]
if not os.path.isabs(path) or not os.path.isabs(database):
    raise SystemExit("owner control paths must be absolute")
flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
source_fd = os.open(path, flags)
metadata = os.fstat(source_fd)
if (
    not stat.S_ISREG(metadata.st_mode)
    or metadata.st_nlink != 1
    or metadata.st_size <= 0
    or metadata.st_size > 8 * 1024 * 1024
    or stat.S_IMODE(metadata.st_mode) & 0o022
    or (hasattr(os, "getuid") and metadata.st_uid != os.getuid())
):
    raise SystemExit("owner control adapter source is unsafe")
source = os.pread(source_fd, metadata.st_size, 0)
if hashlib.sha256(source).hexdigest() != expected:
    raise SystemExit("owner control adapter digest mismatch")
token_read, token_write = os.pipe()
try:
    written = os.write(token_write, token)
    if written != len(token):
        raise SystemExit("owner control token pipe write was incomplete")
finally:
    os.close(token_write)
for name in (
    "RETHLAS_REVIEW_CONTROL_TOKEN",
    "RETHLAS_GUARDIAN_CYCLE_TOKEN",
    "RETHLAS_RUNNER_CYCLE_TOKEN",
    "RETHLAS_STALE_RECOVERY_TOKEN",
):
    os.environ.pop(name, None)
sys.stdin = io.TextIOWrapper(io.BytesIO(envelope), encoding="utf-8")
sys.argv = [
    path,
    "--db",
    database,
    "--control-token-fd",
    str(token_read),
    "--control-token-domain",
    "owner",
    command,
]
namespace = {
    "__builtins__": __builtins__,
    "__file__": path,
    "__name__": "__main__",
    "__package__": None,
    "__spec__": None,
}
exec(compile(bytes(source), path, "exec", dont_inherit=True), namespace, namespace)
PY

run_owner_control() {
  local command="$1"
  local envelope="$2"
  {
    printf '%s\n' "$RETHLAS_REVIEW_CONTROL_TOKEN"
    printf '%s' "$envelope"
  } | "$TRUSTED_PYTHON_BIN" -I -B -c "$OWNER_CONTROL_BOOTSTRAP" \
      "$HOTJOIN_ADAPTER" "$HOTJOIN_ADAPTER_SHA256" "$HOTJOIN_DB" "$command"
}

# The production launcher itself refuses an ordinary path-based execution.
# Load its already-attested bytes through one pinned descriptor, inject the
# descriptor identity into the module namespace, and deliver the owner
# capability through a fresh one-shot FIFO.  The worker receives only the
# launcher's independently generated runner capability.
GUARDIAN_LAUNCH_BOOTSTRAP=''
read -r -d '' GUARDIAN_LAUNCH_BOOTSTRAP <<'PY' || true
import hashlib
import io
import os
import re
import stat
import sys

raw_token = bytearray(sys.stdin.buffer.read(65))
if len(raw_token) != 64 or re.fullmatch(rb"[0-9a-f]{64}", raw_token) is None:
    raise SystemExit("guardian owner capability frame is not exact")
path, expected_sha256 = sys.argv[1:3]
if not os.path.isabs(path) or re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None:
    raise SystemExit("guardian launcher commitment is malformed")
flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
source_fd = os.open(path, flags)
metadata = os.fstat(source_fd)
if (
    not stat.S_ISREG(metadata.st_mode)
    or metadata.st_nlink != 1
    or metadata.st_size <= 0
    or metadata.st_size > 8 * 1024 * 1024
    or stat.S_IMODE(metadata.st_mode) & 0o022
    or (hasattr(os, "getuid") and metadata.st_uid != os.getuid())
):
    raise SystemExit("guardian launcher source is unsafe")
source = bytearray()
offset = 0
while offset < metadata.st_size:
    chunk = os.pread(source_fd, min(131_072, metadata.st_size - offset), offset)
    if not chunk:
        raise SystemExit("guardian launcher source became short")
    source.extend(chunk)
    offset += len(chunk)
if os.pread(source_fd, 1, metadata.st_size):
    raise SystemExit("guardian launcher source grew while pinned")
if hashlib.sha256(source).hexdigest() != expected_sha256:
    raise SystemExit("guardian launcher source digest mismatch")
token_read, token_write = os.pipe()
try:
    view = memoryview(raw_token)
    while view:
        written = os.write(token_write, view)
        if written <= 0:
            raise SystemExit("guardian owner capability FIFO write was short")
        view = view[written:]
finally:
    os.close(token_write)
for index in range(len(raw_token)):
    raw_token[index] = 0
for name in (
    "RETHLAS_REVIEW_CONTROL_TOKEN",
    "RETHLAS_GUARDIAN_CYCLE_TOKEN",
    "RETHLAS_RUNNER_CYCLE_TOKEN",
    "RETHLAS_STALE_RECOVERY_TOKEN",
):
    os.environ.pop(name, None)
sys.stdin = io.TextIOWrapper(io.BytesIO(b""), encoding="utf-8")
sys.argv = [
    path,
    "--owner-token-fd",
    str(token_read),
    *sys.argv[3:],
]
namespace = {
    "__builtins__": __builtins__,
    "__file__": path,
    "__name__": "__main__",
    "__package__": None,
    "__spec__": None,
    "__rethlas_pinned_launcher_fd__": source_fd,
    "__rethlas_pinned_launcher_path__": path,
    "__rethlas_pinned_launcher_sha256__": expected_sha256,
}
exec(compile(bytes(source), path, "exec", dont_inherit=True), namespace, namespace)
PY

run_guardian_launcher() {
  printf '%s' "$RETHLAS_REVIEW_CONTROL_TOKEN" \
    | "$TRUSTED_PYTHON_BIN" -I -B -c "$GUARDIAN_LAUNCH_BOOTSTRAP" \
        "$GUARDIAN_LAUNCHER" "$GUARDIAN_LAUNCHER_SHA256" "$@"
}

guardian_launch_plan() {
  local projection="$1"
  local disposition="$2"
  local status
  if ! status="$(
    "$TRUSTED_PYTHON_BIN" -I -B "$HOTJOIN_ADAPTER" \
      --db "$HOTJOIN_DB" status --run-id "$RETHLAS_HOTJOIN_RUN_ID"
  )"; then
    echo "Could not read the durable generation before Guardian admission." >&2
    return 70
  fi
  RETHLAS_CADENCE_PROJECTION_JSON="$projection" \
  RETHLAS_HOTJOIN_STATUS_JSON="$status" \
    "$TRUSTED_PYTHON_BIN" -I -B - \
      "$RETHLAS_HOTJOIN_RUN_ID" "$problem_rel" "$disposition" <<'PY'
import hashlib
import json
import os
import re
import secrets
import sys


def canonical(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


projection = json.loads(os.environ["RETHLAS_CADENCE_PROJECTION_JSON"])
status = json.loads(os.environ["RETHLAS_HOTJOIN_STATUS_JSON"])
run_id, problem_id, disposition = sys.argv[1:4]
if (
    not isinstance(projection, dict)
    or projection.get("run_id") != run_id
    or projection.get("disposition") != disposition
    or not isinstance(status, dict)
    or status.get("run_id") != run_id
    or status.get("problem_id") != problem_id
    or type(status.get("generation")) is not int
    or int(status["generation"]) < 0
):
    raise SystemExit("Guardian launch projection/status binding is invalid")
watchdog_id = "watchdog_" + secrets.token_hex(16)
review = projection.get("review_cadence")
if not isinstance(review, dict):
    raise SystemExit("Guardian launch projection omitted review cadence")
if disposition == "initial_start_allowed":
    if int(status["generation"]) != 0 or review.get("cycle_id") is not None:
        raise SystemExit("initial Guardian admission is not generation zero")
    admission_mode = "initial_new_cycle"
    expected_generation = 1
    expected_clock = None
elif disposition == "continue_next_cycle":
    admission_mode = "next_new_cycle"
    expected_generation = int(status["generation"]) + 1
    expected_clock = None
elif disposition in {
    "continue_active_cycle",
    "continue_reviewed_cycle_fresh_epoch",
    "resume_active_cycle",
    "review_boundary_recovery_required",
    "review_drive_required",
    "continuous_review_host_recovery",
    "continuous_intent_successor_required",
    "continuous_verdict_successor_required",
}:
    if disposition == "resume_active_cycle" and status.get("active_turn_id") is not None:
        raise SystemExit(
            "an active prior turn must settle under its existing Guardian"
        )
    if disposition == "review_boundary_recovery_required":
        boundary = review.get("review_boundary")
        if not isinstance(boundary, dict) or boundary.get("state") != "root_terminal":
            raise SystemExit(
                "a nonterminal review boundary must settle under its existing Guardian"
            )
    if disposition == "review_drive_required":
        boundary = review.get("review_boundary")
        if (
            not isinstance(boundary, dict)
            or boundary.get("state") != "descendants_terminal"
            or re.fullmatch(
                r"reviewbound_[0-9a-f]{32}", str(boundary.get("boundary_id", ""))
            )
            is None
        ):
            raise SystemExit(
                "a guarded review requires one exact descendants-terminal boundary"
            )
    admission_mode = "same_cycle_resume"
    expected_generation = review.get("generation")
    expected_clock = review.get("guardian_clock_sha256")
    if (
        type(expected_generation) is not int
        or expected_generation < 1
        or re.fullmatch(r"[0-9a-f]{64}", str(expected_clock)) is None
    ):
        raise SystemExit("same-cycle Guardian clock/generation is invalid")
else:
    if disposition == "terminal_observed_pending_finalization":
        raise SystemExit(
            "a pending terminal must be finalized by its existing Guardian"
        )
    raise SystemExit("cadence disposition has no Guardian admission mapping")
if admission_mode == "same_cycle_resume":
    expected_cycle_id = review.get("cycle_id")
    if re.fullmatch(r"cycle_[0-9a-f]{32}", str(expected_cycle_id)) is None:
        raise SystemExit("same-cycle Guardian cycle id is invalid")
else:
    material = {
        "schema_version": "rethlas_guardian_cycle_id_v1",
        "run_id": run_id,
        "generation": expected_generation,
        "watchdog_id": watchdog_id,
    }
    expected_cycle_id = "cycle_" + hashlib.sha256(canonical(material)).hexdigest()[:32]
print(
    "\t".join(
        (
            admission_mode,
            str(expected_cycle_id),
            str(expected_generation),
            "-" if expected_clock is None else str(expected_clock),
            watchdog_id,
        )
    )
)
PY
}

reserve_guarded_log() {
  local path="$1"
  "$TRUSTED_PYTHON_BIN" -I -B - "$ACTIVE_LOG_DIR" "$path" <<'PY'
import os
import stat
import sys
from pathlib import Path

root = Path(sys.argv[1])
target = Path(sys.argv[2])
root_metadata = root.lstat()
if (
    not root.is_absolute()
    or not stat.S_ISDIR(root_metadata.st_mode)
    or stat.S_IMODE(root_metadata.st_mode) != 0o700
    or target.parent != root
    or target.name in {"", ".", ".."}
):
    raise SystemExit("guarded log path is outside its owner invocation")
flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
descriptor = os.open(target, flags, 0o600)
try:
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600:
        raise SystemExit("guarded log reservation is unsafe")
    os.fsync(descriptor)
finally:
    os.close(descriptor)
directory = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
try:
    os.fsync(directory)
finally:
    os.close(directory)
PY
}

control_capability_bind() {
  local envelope response expected_token_sha256
  expected_token_sha256="$(
    printf '%s' "$RETHLAS_REVIEW_CONTROL_TOKEN" \
      | "$TRUSTED_PYTHON_BIN" -I -B -c \
          'import hashlib, sys; print(hashlib.sha256(sys.stdin.buffer.read()).hexdigest())'
  )" || return 70
  envelope="$(
    "$TRUSTED_PYTHON_BIN" -I -B - \
      "$RETHLAS_HOTJOIN_RUN_ID" "$REVIEW_CONTRACT_CLI_PATH" \
      "$REVIEW_CONTRACT_CLI_SHA256" "$TRUSTED_RUNTIME_MANIFEST" \
      "$REVIEW_DRIVER_PATH" "$REVIEW_DRIVER_SHA256" \
      "$REVIEW_DRIVER_PACKAGE_SHA256" \
      "$MODEL" "$REASONING_EFFORT" "$REVIEW_POLICY_SHA256" \
      "$CODEX_BIN" "$CODEX_BIN_SHA256" \
      "$RETHLAS_GENERATION_CONTROL_TOKEN" \
      "$RETHLAS_EXPECTED_STATEMENT_SHA256" <<'PY'
import json
import sys

keys = (
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
)
payload = dict(zip(keys, sys.argv[1:], strict=True))
value = {
    "schema_version": "rethlas_review_adapter_command_v1",
    "command": "control_capability_bind",
    "payload": payload,
}
print(json.dumps(value, allow_nan=False, sort_keys=True, separators=(",", ":")))
PY
  )"
  if ! response="$(run_owner_control control-capability-bind "$envelope")"; then
    echo "Could not bind the scoped cadence control capability." >&2
    return 70
  fi
  RETHLAS_CONTROL_BINDING_JSON="$response" \
    "$TRUSTED_PYTHON_BIN" -I -B - \
      "$RETHLAS_HOTJOIN_RUN_ID" "$REVIEW_CONTRACT_CLI_SHA256" \
      "$TRUSTED_RUNTIME_MANIFEST" "$REVIEW_DRIVER_SHA256" \
      "$REVIEW_DRIVER_PACKAGE_SHA256" \
      "$RETHLAS_GENERATION_CONTROL_TOKEN" "$expected_token_sha256" <<'PY'
import json
import os
import re
import sys

try:
    value = json.loads(os.environ["RETHLAS_CONTROL_BINDING_JSON"])
except (json.JSONDecodeError, UnicodeError) as exc:
    print(f"invalid control capability binding response: {exc}", file=sys.stderr)
    raise SystemExit(70)
expected = {
    "schema_version",
    "run_id",
    "state",
    "capability_revision",
    "token_sha256",
    "contract_cli_sha256",
    "trusted_runtime_sha256",
    "review_driver_sha256",
    "review_driver_package_sha256",
    "generation_control_instance_id",
}
if not isinstance(value, dict) or set(value) != expected:
    raise SystemExit(70)
if (
    value["schema_version"] != "rethlas_control_capability_binding_v1"
    or value["run_id"] != sys.argv[1]
    or value["state"] not in {"bound", "rotated"}
    or not isinstance(value["capability_revision"], int)
    or isinstance(value["capability_revision"], bool)
    or value["capability_revision"] < 1
    or value["contract_cli_sha256"] != sys.argv[2]
    or value["trusted_runtime_sha256"] != sys.argv[3]
    or value["review_driver_sha256"] != sys.argv[4]
    or value["review_driver_package_sha256"] != sys.argv[5]
    or value["generation_control_instance_id"] != sys.argv[6]
    or re.fullmatch(r"[0-9a-f]{64}", value["token_sha256"] or "") is None
    or value["token_sha256"] != sys.argv[7]
):
    raise SystemExit(70)
print(value["capability_revision"])
PY
}

cadence_admit() {
  local operation="$1"
  local generation_receipt="$2"
  local envelope response
  envelope="$(
    RETHLAS_GENERATION_CONTROL_RECEIPT_JSON="$generation_receipt" \
      "$TRUSTED_PYTHON_BIN" -I -B - \
        "$operation" "$RETHLAS_HOTJOIN_RUN_ID" <<'PY'
import json
import os
import sys

value = {
    "schema_version": "rethlas_review_adapter_command_v1",
    "command": "cadence_admit",
    "payload": {
        "operation": sys.argv[1],
        "run_id": sys.argv[2],
        "generation_control_receipt": json.loads(
            os.environ["RETHLAS_GENERATION_CONTROL_RECEIPT_JSON"]
        ),
    },
}
print(json.dumps(value, allow_nan=False, sort_keys=True, separators=(",", ":")))
PY
  )"
  if ! response="$(run_owner_control cadence-admit "$envelope")"; then
    echo "Cadence admission failed for operation=$operation." >&2
    return 70
  fi
  validate_cadence_projection "$response"
}

cadence_close_owner_yield() {
  local generation_receipt="$1"
  local projection="$2"
  local envelope response
  envelope="$(
    RETHLAS_GENERATION_CONTROL_RECEIPT_JSON="$generation_receipt" \
    RETHLAS_CADENCE_PROJECTION_JSON="$projection" \
      "$TRUSTED_PYTHON_BIN" -I -B - \
        "$RETHLAS_HOTJOIN_RUN_ID" <<'PY'
import json
import os
import re
import sys

projection = json.loads(os.environ["RETHLAS_CADENCE_PROJECTION_JSON"])
receipt = json.loads(os.environ["RETHLAS_GENERATION_CONTROL_RECEIPT_JSON"])
review = projection["review_cadence"]
epoch = projection["thread_epoch"]
if not isinstance(review, dict) or not isinstance(review.get("cycle_id"), str):
    raise SystemExit("cadence owner yield lacks a current cycle id")
if not isinstance(epoch, dict) or set(epoch) != {
    "active_turn_id",
    "handoff_id",
    "handoff_sha256",
    "predecessor_epoch",
    "state",
    "thread_epoch",
    "thread_id",
}:
    raise SystemExit("cadence owner yield lacks a pending handoff epoch")
if (
    epoch["state"] != "pending"
    or epoch["thread_id"] is not None
    or epoch["active_turn_id"] is not None
    or not isinstance(epoch["thread_epoch"], int)
    or isinstance(epoch["thread_epoch"], bool)
    or not isinstance(epoch["handoff_sha256"], str)
    or re.fullmatch(r"[0-9a-f]{64}", epoch["handoff_sha256"]) is None
    or epoch["handoff_id"] != f"handoff_{epoch['handoff_sha256']}"
):
    raise SystemExit("cadence owner yield handoff binding is invalid")
value = {
    "schema_version": "rethlas_review_adapter_command_v1",
    "command": "cadence_close",
    "payload": {
        "operation": "owner_yield",
        "run_id": sys.argv[1],
        "cycle_id": review["cycle_id"],
        "handoff_id": epoch["handoff_id"],
        "content_sha256": epoch["handoff_sha256"],
        "to_thread_epoch": epoch["thread_epoch"],
        "generation_control_receipt": receipt,
    },
}
print(json.dumps(value, allow_nan=False, sort_keys=True, separators=(",", ":")))
PY
  )" || return 70
  if ! response="$(run_owner_control cadence-close "$envelope")"; then
    echo "Could not close the cadence cycle for the authenticated owner yield." >&2
    return 70
  fi
  validate_cadence_projection "$response"
}

cadence_close_verified_completion() {
  local evidence envelope response disposition
  if ! evidence="$(
    RETHLAS_EMIT_COMPLETION_EVIDENCE=1 receipt_is_valid
  )"; then
    echo "Could not reconstruct verified completion evidence." >&2
    return 70
  fi
  envelope="$(
    RETHLAS_COMPLETION_EVIDENCE_JSON="$evidence" \
      "$TRUSTED_PYTHON_BIN" -I -B - \
        "$RETHLAS_HOTJOIN_RUN_ID" <<'PY'
import json
import os
import sys

evidence = json.loads(os.environ["RETHLAS_COMPLETION_EVIDENCE_JSON"])
value = {
    "schema_version": "rethlas_review_adapter_command_v1",
    "command": "cadence_close",
    "payload": {
        "operation": "verified_completion",
        "run_id": sys.argv[1],
        **{
            key: evidence[key]
            for key in (
                "problem_id",
                "statement_sha256",
                "publication_receipt_sha256",
                "verified_proof_sha256",
                "published_bytes",
            )
        },
    },
}
print(json.dumps(value, allow_nan=False, sort_keys=True, separators=(",", ":")))
PY
  )" || return 70
  if ! response="$(run_owner_control cadence-close "$envelope")"; then
    echo "Could not close the continuous supervisor after verified publication." >&2
    return 70
  fi
  validate_cadence_projection "$response" || return 70
  disposition="$(cadence_projection_disposition "$response")" || return 70
  if [[ "$disposition" != completed ]]; then
    echo "Verified continuous completion did not reach completed state." >&2
    return 70
  fi
}

review_drive_due() {
  local projection="$1"
  local boundary_id guardian_plan guardian_admission_mode
  local guardian_expected_cycle_id guardian_expected_generation
  local guardian_expected_clock_sha256 guardian_watchdog_id
  local response post_projection review_log review_rc
  local -a review_worker_command review_guardian_command
  boundary_id="$(
    RETHLAS_CADENCE_PROJECTION_JSON="$projection" \
      "$TRUSTED_PYTHON_BIN" -I -B - <<'PY'
import json
import os
import re
import sys

projection = json.loads(os.environ["RETHLAS_CADENCE_PROJECTION_JSON"])
review = projection.get("review_cadence")
boundary = review.get("review_boundary") if isinstance(review, dict) else None
if (
    projection.get("disposition") != "review_drive_required"
    or not isinstance(boundary, dict)
    or boundary.get("state") != "descendants_terminal"
    or re.fullmatch(
        r"reviewbound_[0-9a-f]{32}", str(boundary.get("boundary_id", ""))
    )
    is None
):
    raise SystemExit("review drive lacks an exact descendants-terminal boundary")
print(boundary["boundary_id"])
PY
  )" || return 70
  # Starting the root thread epoch advances the durable review-control fence.
  # Rebind the same owner capability after the root Guardian has finalized so
  # the due-review Guardian receives the current revision rather than the
  # wrapper's pre-thread cached value.  The raw token and scope do not change.
  if ! CONTROL_CAPABILITY_REVISION="$(control_capability_bind)"; then
    echo "Could not refresh the scoped control fence for the due review." >&2
    return 70
  fi
  if ! [[ "$CONTROL_CAPABILITY_REVISION" =~ ^[1-9][0-9]*$ ]]; then
    echo "Due-review control fence refresh returned an invalid revision." >&2
    return 70
  fi
  if ! guardian_plan="$(guardian_launch_plan "$projection" review_drive_required)"; then
    echo "Could not derive the exact Guardian admission for the due review." >&2
    return 70
  fi
  IFS=$'\t' read -r guardian_admission_mode guardian_expected_cycle_id \
    guardian_expected_generation guardian_expected_clock_sha256 \
    guardian_watchdog_id <<< "$guardian_plan"
  if [[ "$guardian_admission_mode" != same_cycle_resume \
     || ! "$guardian_expected_cycle_id" =~ ^cycle_[0-9a-f]{32}$ \
     || ! "$guardian_expected_generation" =~ ^[1-9][0-9]*$ \
     || ! "$guardian_expected_clock_sha256" =~ ^[0-9a-f]{64}$ \
     || ! "$guardian_watchdog_id" =~ ^watchdog_[0-9a-f]{32}$ ]]; then
    echo "Due-review Guardian admission is malformed." >&2
    return 70
  fi
  review_worker_command=(
    "$TRUSTED_PYTHON_BIN" "$HOTJOIN_ADAPTER"
    --db "$HOTJOIN_DB"
    guarded-review-drive
    --run-id "$RETHLAS_HOTJOIN_RUN_ID"
    --boundary-id "$boundary_id"
  )
  review_guardian_command=(
    --db "$HOTJOIN_DB"
    --adapter-path "$HOTJOIN_ADAPTER"
    --adapter-sha256 "$HOTJOIN_ADAPTER_SHA256"
    --guardian-path "$GUARDIAN_SOURCE"
    --runner-path "$GUARDIAN_RUNNER"
    --run-id "$RETHLAS_HOTJOIN_RUN_ID"
    --generation-control-instance-id "$RETHLAS_GENERATION_CONTROL_TOKEN"
    --watchdog-id "$guardian_watchdog_id"
    --admission-mode "$guardian_admission_mode"
    --expected-cycle-id "$guardian_expected_cycle_id"
    --expected-generation "$guardian_expected_generation"
    --expected-clock-sha256 "$guardian_expected_clock_sha256"
    --capability-revision "$CONTROL_CAPABILITY_REVISION"
    --policy-contract-sha256 "$POLICY_CONTRACT_SHA256"
    --policy-digest "$REVIEW_POLICY_SHA256"
    --worker-cwd "$ROOT_DIR"
    --problem-path "$ROOT_DIR/$PROBLEM_FILE"
    --problem-relative-path "$PROBLEM_FILE"
    --worker-mode runner_control
    -- "${review_worker_command[@]}"
  )
  review_log="$ACTIVE_LOG_DIR/review_${boundary_id}.jsonl"
  if ! reserve_guarded_log "$review_log"; then
    echo "Could not reserve a fresh guarded log for boundary=$boundary_id." >&2
    return 70
  fi
  if ! hotjoin_control_sources_unchanged; then
    echo "Trusted review-driver/control sources changed before guarded review; zero reviewer/root turns were started." >&2
    return 70
  fi
  if (
    cd "$ROOT_DIR"
    run_guardian_launcher "${review_guardian_command[@]}"
  ) >>"$review_log" 2>&1; then
    review_rc=0
  else
    review_rc=$?
  fi
  if [[ "$review_rc" -ne 0 ]]; then
    echo "Guarded review-drive failed closed with code $review_rc (see $review_log)." >&2
    return 70
  fi
  if ! hotjoin_control_sources_unchanged; then
    echo "Trusted review-driver/control sources changed during guarded review; refusing its result." >&2
    return 70
  fi
  if ! response="$(
    "$TRUSTED_PYTHON_BIN" -I -B - "$review_log" <<'PY'
import json
import os
import stat
import sys


def strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


path = os.path.abspath(sys.argv[1])
flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
descriptor = os.open(path, flags)
try:
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_nlink != 1
        or metadata.st_size <= 0
        or metadata.st_size > 16 * 1024 * 1024
        or (hasattr(os, "getuid") and metadata.st_uid != os.getuid())
    ):
        raise SystemExit("guarded review log is unsafe")
    raw = os.read(descriptor, metadata.st_size + 1)
finally:
    os.close(descriptor)
if len(raw) != metadata.st_size:
    raise SystemExit("guarded review log changed during read")
records: list[dict[str, object]] = []
for line in raw.splitlines():
    if not line:
        continue
    value = json.loads(
        line.decode("utf-8"),
        object_pairs_hook=strict_object,
        parse_constant=lambda value: (_ for _ in ()).throw(
            ValueError(f"invalid JSON constant: {value}")
        ),
    )
    if not isinstance(value, dict):
        raise SystemExit("guarded review log contains a non-object record")
    if value.get("schema_version") == "rethlas_review_drive_result_v1":
        records.append(value)
if len(records) != 1:
    raise SystemExit("guarded review log does not contain one exact result")
print(
    json.dumps(
        records[0],
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
)
PY
  )"; then
    echo "Could not recover the exact guarded review result from $review_log." >&2
    return 70
  fi
  RETHLAS_REVIEW_DRIVE_RESULT_JSON="$response" \
  RETHLAS_CADENCE_PROJECTION_JSON="$projection" \
    "$TRUSTED_PYTHON_BIN" -I -B - \
      "$RETHLAS_HOTJOIN_RUN_ID" <<'PY'
import hashlib
import json
import os
import re
import sys


def fail(message: str) -> None:
    print(f"invalid owner review-drive result: {message}", file=sys.stderr)
    raise SystemExit(70)


try:
    value = json.loads(os.environ["RETHLAS_REVIEW_DRIVE_RESULT_JSON"])
    before = json.loads(os.environ["RETHLAS_CADENCE_PROJECTION_JSON"])
except (json.JSONDecodeError, UnicodeError) as exc:
    fail(f"not strict JSON: {exc}")
expected = {
    "schema_version",
    "run_id",
    "boundary_id",
    "cycle_id",
    "review_id",
    "state",
    "disposition_sha256",
    "disposition",
    "review_cadence",
    "thread_epoch",
}
if not isinstance(value, dict) or set(value) != expected:
    fail("top-level fields mismatch")
boundary = before["review_cadence"]["review_boundary"]
if (
    value["schema_version"] != "rethlas_review_drive_result_v1"
    or value["run_id"] != sys.argv[1]
    or value["boundary_id"] != boundary["boundary_id"]
    or value["cycle_id"] != before["review_cadence"].get("cycle_id")
    or re.fullmatch(r"cycle_[0-9a-f]{32}", value["cycle_id"] or "") is None
    or re.fullmatch(r"review_[0-9a-f]{32}", value["review_id"] or "") is None
    or not isinstance(value["review_cadence"], dict)
):
    fail("host boundary/cycle/review binding mismatch")
state = value["state"]
if state == "disposition_ready":
    disposition = value["disposition"]
    if (
        not isinstance(disposition, dict)
        or set(disposition)
        != {
            "schema_version",
            "review_id",
            "request_sha256",
            "snapshot_sha256",
            "decision",
            "active_route",
            "frozen_route_id",
            "route_transition_publication_receipt",
            "next_milestone",
            "evidence_record_ids",
            "requires_targeted_verification",
        }
        or disposition.get("schema_version") != "rethlas_review_disposition_v1"
        or disposition.get("review_id") != value["review_id"]
        or re.fullmatch(r"[0-9a-f]{64}", disposition.get("request_sha256", ""))
        is None
        or re.fullmatch(r"[0-9a-f]{64}", disposition.get("snapshot_sha256", ""))
        is None
        or disposition.get("requires_targeted_verification") is not False
        or re.fullmatch(r"[0-9a-f]{64}", value["disposition_sha256"] or "")
        is None
    ):
        fail("official disposition is malformed")
    encoded = json.dumps(
        disposition,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if hashlib.sha256(encoded).hexdigest() != value["disposition_sha256"]:
        fail("official disposition digest mismatch")
elif state in {"operational_blocked", "execution_unknown", "verification_unknown"}:
    if value["disposition"] is not None or value["disposition_sha256"] is not None:
        fail("failed review drive cannot carry a disposition")
else:
    fail("review drive returned a nonterminal state")
PY
  post_projection="$(cadence_control_projection)" || return 70
  RETHLAS_REVIEW_DRIVE_RESULT_JSON="$response" \
  RETHLAS_CADENCE_PROJECTION_JSON="$post_projection" \
    "$TRUSTED_PYTHON_BIN" -I -B - <<'PY'
import json
import os

result = json.loads(os.environ["RETHLAS_REVIEW_DRIVE_RESULT_JSON"])
projection = json.loads(os.environ["RETHLAS_CADENCE_PROJECTION_JSON"])
if (
    result["review_cadence"] != projection["review_cadence"]
    or result["thread_epoch"] != projection["thread_epoch"]
):
    raise SystemExit("review-drive result differs from durable cadence projection")
if result["state"] == "disposition_ready":
    if projection["disposition"] == "continue_reviewed_cycle_fresh_epoch":
        if (
            projection["paid_turn_allowed"] is not True
            or projection["context_guard"]["adapter_resume_allowed"] is not True
            or not isinstance(projection["thread_epoch"], dict)
            or projection["thread_epoch"].get("state") != "pending"
            or projection["thread_epoch"].get("thread_id") is not None
        ):
            raise SystemExit(
                "official review did not close into a host-prepared fresh epoch"
            )
    elif projection["disposition"] == "route_frozen":
        if (
            projection["paid_turn_allowed"] is not False
            or projection["context_guard"]["adapter_resume_allowed"] is not False
        ):
            raise SystemExit("frozen review route exposed continuation authority")
    else:
        raise SystemExit("official review ended in an unsupported cadence disposition")
elif projection["paid_turn_allowed"] is not False:
    raise SystemExit("failed review drive exposed a paid turn")
PY
  # This function must run in the caller's shell. In addition to returning the
  # durable projection, it refreshes CONTROL_CAPABILITY_REVISION before the
  # guarded reviewer starts. Keeping both values in caller-visible globals
  # prevents the next fresh-epoch Guardian from using the stale pre-review
  # revision after a command-substitution subshell exits.
  REVIEW_DRIVE_PROJECTION="$post_projection"
}

cadence_start_disposition() {
  local stage="$1"
  local projection="${2:-}"
  local disposition
  if [[ -z "$projection" ]]; then
    projection="$(cadence_control_projection)" || return $?
  fi
  if ! disposition="$(
    RETHLAS_CADENCE_PROJECTION_JSON="$projection" \
      "$TRUSTED_PYTHON_BIN" -I -B - "$stage" <<'PY'
import json
import os
import re
import sys

value = json.loads(os.environ["RETHLAS_CADENCE_PROJECTION_JSON"])
stage = sys.argv[1]
disposition = value["disposition"]
paid_starts = {
    "initial_start_allowed",
    "continue_active_cycle",
    "continue_next_cycle",
    "continue_reviewed_cycle_fresh_epoch",
    "continuous_intent_successor_required",
    "continuous_verdict_successor_required",
}
recovery_only = {"resume_active_cycle", "terminal_observed_pending_finalization"}
recovery_only.add("review_boundary_recovery_required")
recovery_only.add("continuous_review_host_recovery")
normal_terminals = {"hard_stopped", "route_frozen", "stop_unsolved"}
continue_epoch_valid = True
if disposition in {"continue_next_cycle", "continue_reviewed_cycle_fresh_epoch"}:
    epoch = value["thread_epoch"]
    epoch_fields = {
        "active_turn_id",
        "handoff_id",
        "handoff_sha256",
        "predecessor_epoch",
        "state",
        "thread_epoch",
        "thread_id",
    }
    continue_epoch_valid = (
        isinstance(epoch, dict)
        and set(epoch) == epoch_fields
        and epoch["active_turn_id"] is None
        and epoch["thread_id"] is None
        and epoch["state"] == "pending"
        and isinstance(epoch["predecessor_epoch"], int)
        and not isinstance(epoch["predecessor_epoch"], bool)
        and epoch["predecessor_epoch"] >= 1
        and isinstance(epoch["thread_epoch"], int)
        and not isinstance(epoch["thread_epoch"], bool)
        and epoch["thread_epoch"] == epoch["predecessor_epoch"] + 1
        and isinstance(epoch["handoff_sha256"], str)
        and re.fullmatch(r"[0-9a-f]{64}", epoch["handoff_sha256"]) is not None
        and epoch["handoff_id"] == f"handoff_{epoch['handoff_sha256']}"
    )
elif disposition == "continuous_intent_successor_required":
    epoch = value["thread_epoch"]
    epoch_fields = {
        "active_turn_id",
        "handoff_id",
        "handoff_sha256",
        "predecessor_epoch",
        "state",
        "thread_epoch",
        "thread_id",
    }
    active_epoch_valid = bool(
        isinstance(epoch, dict)
        and set(epoch) == epoch_fields
        and epoch["active_turn_id"] is None
        and epoch["state"] == "active"
        and isinstance(epoch["thread_id"], str)
        and bool(epoch["thread_id"])
        and isinstance(epoch["thread_epoch"], int)
        and not isinstance(epoch["thread_epoch"], bool)
        and epoch["thread_epoch"] >= 1
    )
    pending_epoch_valid = bool(
        isinstance(epoch, dict)
        and set(epoch) == epoch_fields
        and epoch["active_turn_id"] is None
        and epoch["thread_id"] is None
        and epoch["state"] == "pending"
        and isinstance(epoch["predecessor_epoch"], int)
        and not isinstance(epoch["predecessor_epoch"], bool)
        and isinstance(epoch["thread_epoch"], int)
        and epoch["thread_epoch"] == epoch["predecessor_epoch"] + 1
        and isinstance(epoch["handoff_sha256"], str)
        and re.fullmatch(r"[0-9a-f]{64}", epoch["handoff_sha256"]) is not None
        and epoch["handoff_id"] == f"handoff_{epoch['handoff_sha256']}"
    )
    continue_epoch_valid = active_epoch_valid or pending_epoch_valid
elif disposition in {"continue_active_cycle", "continuous_verdict_successor_required"}:
    epoch = value["thread_epoch"]
    epoch_fields = {
        "active_turn_id",
        "handoff_id",
        "handoff_sha256",
        "predecessor_epoch",
        "state",
        "thread_epoch",
        "thread_id",
    }
    active_handoff_valid = (
        epoch.get("handoff_id") is None
        and epoch.get("handoff_sha256") is None
    ) if isinstance(epoch, dict) else False
    if isinstance(epoch, dict) and isinstance(epoch.get("handoff_sha256"), str):
        active_handoff_valid = (
            re.fullmatch(r"[0-9a-f]{64}", epoch["handoff_sha256"]) is not None
            and epoch.get("handoff_id") == f"handoff_{epoch['handoff_sha256']}"
            and isinstance(epoch.get("predecessor_epoch"), int)
            and not isinstance(epoch["predecessor_epoch"], bool)
            and isinstance(epoch.get("thread_epoch"), int)
            and epoch["predecessor_epoch"] == epoch["thread_epoch"] - 1
        )
    continue_epoch_valid = (
        isinstance(epoch, dict)
        and set(epoch) == epoch_fields
        and epoch["active_turn_id"] is None
        and active_handoff_valid
        and epoch["state"] == "active"
        and isinstance(epoch["thread_id"], str)
        and bool(epoch["thread_id"])
        and isinstance(epoch["thread_epoch"], int)
        and not isinstance(epoch["thread_epoch"], bool)
        and epoch["thread_epoch"] >= 1
    )
if (
    disposition not in paid_starts | recovery_only | normal_terminals
    or value["quarantine"] is not None
    or not continue_epoch_valid
    or (
        disposition in paid_starts
        and (
            value["paid_turn_allowed"] is not True
            or value["context_guard"]["adapter_resume_allowed"] is not True
        )
    )
    or (
        disposition in recovery_only
        and (
            value["paid_turn_allowed"] is not False
            or value["context_guard"]["adapter_resume_allowed"] is not True
        )
    )
    or (
        disposition in normal_terminals
        and (
            value["paid_turn_allowed"] is not False
            or value["context_guard"]["adapter_resume_allowed"] is not False
        )
    )
    or (stage == "next" and disposition == "initial_start_allowed")
    or (
        disposition == "review_boundary_recovery_required"
        and (
            not isinstance(value["review_cadence"].get("review_boundary"), dict)
            or value["review_cadence"]["review_boundary"]["state"]
            not in {"root_dispatching", "root_accepted", "root_terminal"}
        )
    )
):
    print(
        f"durable cadence disposition {disposition!r} forbids a paid turn",
        file=sys.stderr,
    )
    raise SystemExit(70)
print(disposition)
PY
  )"; then
    return 70
  fi
  printf '%s\n' "$disposition"
}

cadence_projection_cycle_id() {
  local projection="$1"
  RETHLAS_CADENCE_PROJECTION_JSON="$projection" \
    "$TRUSTED_PYTHON_BIN" -I -B - <<'PY'
import json
import os
import re

try:
    value = json.loads(os.environ["RETHLAS_CADENCE_PROJECTION_JSON"])
except (json.JSONDecodeError, UnicodeError) as exc:
    raise SystemExit(f"invalid cadence projection while reading cycle id: {exc}")
review = value.get("review_cadence")
if not isinstance(review, dict):
    raise SystemExit("cadence projection has no review_cadence object")
cycle_id = review.get("cycle_id")
if cycle_id is None:
    print("")
elif isinstance(cycle_id, str) and re.fullmatch(r"cycle_[0-9a-f]{32}", cycle_id):
    print(cycle_id)
else:
    raise SystemExit("cadence projection cycle_id is malformed")
PY
}

START_EPOCH=$(date +%s)

# This read happens before generation_control_resume, capability rotation, and
# any paid turn. A wrapper restart therefore cannot clear an old stop/yield or
# rotate into a blocked run. The adapter's persisted cycle T0 and disposition
# are the only cadence clock/permission authority.
initial_cadence_disposition=""
if [[ "$REVIEW_CADENCE_POLICY" == rethlas_route_review_150m_v2 ]]; then
  initial_projection="$(cadence_control_projection)" || exit 70
  initial_pre_disposition="$(
    cadence_projection_disposition "$initial_projection"
  )" || exit 70
  case "$initial_pre_disposition" in
    hard_stopped)
      echo "The theorem remains unsolved; durable cadence is already at its finalized T+150m hard stop (state=hard_stopped)." >&2
      echo "No recovery or additional paid cycle is authorized." >&2
      exit 1
      ;;
    route_frozen)
      echo "The theorem remains unsolved; the active route is durably frozen after an official red verdict with no authorized fallback (state=route_frozen)." >&2
      echo "This is not an owner/advisor wait and authorizes no additional paid work." >&2
      exit 1
      ;;
    review_turn_authorization_required|continue_review_only)
      echo "A due route review requires trusted host orchestration; an ordinary full-capability generator turn is forbidden." >&2
      echo "No root model turn was started." >&2
      exit 70
      ;;
    post_review_handoff_required)
      echo "The official route review is closed, but its host-prepared fresh-epoch handoff is not yet available." >&2
      echo "No root model turn was started." >&2
      exit 70
      ;;
    owner_yield_close_required)
      echo "A prior generation yield still requires authenticated host cadence-close recovery; its wait receipt will not be overwritten." >&2
      echo "No root model turn was started." >&2
      exit 70
      ;;
    initial_start_allowed|continuation_authorization_required|owner_wait_cost|owner_wait_advisor|continue_active_cycle|continue_next_cycle|continue_reviewed_cycle_fresh_epoch|resume_active_cycle|terminal_observed_pending_finalization|review_boundary_recovery_required|review_drive_required)
      ;;
    *)
      echo "Durable cadence state=$initial_pre_disposition is blocked or unknown; no model or recovery turn is authorized." >&2
      exit 70
      ;;
  esac
  if [[ "$initial_pre_disposition" == terminal_observed_pending_finalization ]]; then
    echo "The prior root terminal is still settling under its existing Guardian; refusing capability rotation or a second root." >&2
    exit 70
  fi
  if [[ "$initial_pre_disposition" == resume_active_cycle \
     || "$initial_pre_disposition" == review_boundary_recovery_required ]]; then
    if ! guardian_launch_plan "$initial_projection" "$initial_pre_disposition" >/dev/null; then
      echo "The prior Guardian/root is not yet clean enough for same-cycle recovery; refusing capability rotation." >&2
      exit 70
    fi
  fi
  if ! hotjoin_control_sources_unchanged; then
    echo "Trusted control/helper/Codex sources changed before capability binding; refusing to start." >&2
    exit 70
  fi
  if ! CONTROL_CAPABILITY_REVISION="$(control_capability_bind)"; then
    exit 70
  fi
  if ! [[ "$CONTROL_CAPABILITY_REVISION" =~ ^[1-9][0-9]*$ ]]; then
    echo "Scoped cadence control binding returned an invalid capability revision." >&2
    exit 70
  fi
  if ! hotjoin_control_sources_unchanged; then
    echo "Trusted control/helper/Codex sources changed during capability binding; refusing to start." >&2
    exit 70
  fi

  # Capability binding also performs the atomic recovery of an expired
  # Guardian prepare that never registered. Re-read the durable projection so
  # a wrapper restart observes that transaction instead of requiring a manual
  # second invocation. A genuinely active/pending prior Guardian remains a
  # hard refusal after the bind attempt.
  initial_projection="$(cadence_control_projection)" || exit 70
  initial_pre_disposition="$(
    cadence_projection_disposition "$initial_projection"
  )" || exit 70
  case "$initial_pre_disposition" in
    terminal_observed_pending_finalization)
      echo "The prior root terminal is still settling under its existing Guardian; refusing capability rotation or a second root." >&2
      exit 70
      ;;
    hard_stopped|route_frozen)
      echo "Durable cadence became terminal during capability recovery (state=$initial_pre_disposition); no additional paid root is authorized." >&2
      exit 1
      ;;
    initial_start_allowed|continuation_authorization_required|owner_wait_cost|owner_wait_advisor|continue_active_cycle|continue_next_cycle|continue_reviewed_cycle_fresh_epoch|resume_active_cycle|review_boundary_recovery_required|review_drive_required)
      ;;
    *)
      echo "Durable cadence changed to blocked state=$initial_pre_disposition during capability recovery; no paid root is authorized." >&2
      exit 70
      ;;
  esac

  if [[ "$initial_pre_disposition" == review_drive_required ]]; then
    REVIEW_DRIVE_PROJECTION=""
    if ! review_drive_due "$initial_projection"; then
      exit 70
    fi
    initial_projection="$REVIEW_DRIVE_PROJECTION"
    initial_pre_disposition="$(
      cadence_projection_disposition "$initial_projection"
    )" || exit 70
    if [[ "$initial_pre_disposition" == continue_reviewed_cycle_fresh_epoch ]]; then
      echo "The due route review closed under trusted host orchestration; its authenticated same-cycle fresh epoch is ready." >&2
    elif [[ "$initial_pre_disposition" == route_frozen ]]; then
      echo "The theorem remains unsolved; the official review froze the active route after red with no authorized fallback (state=route_frozen)." >&2
      echo "No owner/advisor wait or paid root continuation was created." >&2
      exit 1
    else
      echo "Trusted host review orchestration ended in state=$initial_pre_disposition; no root turn is authorized." >&2
      exit 70
    fi
  fi

  # Starting this wrapper is the repository owner's explicit action. The
  # receipt is written only after the pre-existing cadence state has admitted
  # this host instance, and still cannot authorize a paid turn by itself.
  if ! generation_control_resume; then
    echo "Could not durably resume generation control; refusing to start Codex." >&2
    exit 70
  fi
  initial_generation_receipt="$(generation_control_receipt)" || exit 70
  case "$initial_pre_disposition" in
    continuation_authorization_required|owner_wait_cost|owner_wait_advisor)
      if receipt_is_valid; then
        echo "Solved problem_id=$problem_rel before cadence admission"
        exit 0
      fi
      case "$initial_pre_disposition" in
        continuation_authorization_required)
          cadence_admit continue_active_cycle "$initial_generation_receipt" >/dev/null \
            || exit 70
          ;;
        owner_wait_cost|owner_wait_advisor)
          cadence_admit owner_resume "$initial_generation_receipt" >/dev/null \
            || exit 70
          ;;
      esac
      ;;
  esac
  if ! initial_cadence_disposition="$(cadence_start_disposition initial)"; then
    echo "Durable cadence state does not authorize a paid cycle; refusing to start Codex." >&2
    exit 70
  fi
elif [[ "$REVIEW_CADENCE_POLICY" == rethlas_continuous_supervisor_v1 ]]; then
  if ! hotjoin_control_sources_unchanged; then
    echo "Trusted continuous-supervisor sources changed before capability binding." >&2
    exit 70
  fi
  if ! CONTROL_CAPABILITY_REVISION="$(control_capability_bind)"; then
    exit 70
  fi
  if ! [[ "$CONTROL_CAPABILITY_REVISION" =~ ^[1-9][0-9]*$ ]]; then
    echo "Continuous-supervisor control binding returned an invalid revision." >&2
    exit 70
  fi
  if ! generation_control_resume; then
    echo "Could not durably resume continuous generation control." >&2
    exit 70
  fi
  initial_projection="$(cadence_control_projection)" || exit 70
  initial_pre_disposition="$(
    cadence_projection_disposition "$initial_projection"
  )" || exit 70
  if receipt_is_valid; then
    case "$initial_pre_disposition" in
      completed|initial_start_allowed)
        echo "Solved problem_id=$problem_rel before continuous admission"
        exit 0
        ;;
      continuous_root_missing)
        cadence_close_verified_completion || exit 70
        echo "Solved problem_id=$problem_rel during continuous completion recovery"
        exit 0
        ;;
    esac
  fi
  case "$initial_pre_disposition" in
    owner_wait_cost|owner_wait_advisor)
      initial_generation_receipt="$(generation_control_receipt)" || exit 70
      cadence_admit owner_resume "$initial_generation_receipt" >/dev/null \
        || exit 70
      initial_projection="$(cadence_control_projection)" || exit 70
      initial_pre_disposition="$(
        cadence_projection_disposition "$initial_projection"
      )" || exit 70
      if [[ "$initial_pre_disposition" != continue_next_cycle ]]; then
        echo "Continuous owner resume did not produce one fresh-epoch transport admission." >&2
        exit 70
      fi
      ;;
    initial_start_allowed|continue_next_cycle|continuous_review_host_recovery|continuous_intent_successor_required|continuous_verdict_successor_required)
      ;;
    owner_yield_close_required)
      echo "A prior continuous generation yield still requires authenticated close; no root was started." >&2
      exit 70
      ;;
    stop_unsolved)
      echo "The continuous run is already at durable stop_unsolved; no paid successor is authorized." >&2
      exit 1
      ;;
    continuous_active|continuous_start_in_progress|terminal_observed_pending_finalization)
      echo "A continuous root or its existing Guardian is still active; refusing a duplicate root." >&2
      exit 70
      ;;
    *)
      echo "Continuous supervisor is not startable from state=$initial_pre_disposition." >&2
      exit 70
      ;;
  esac
  initial_cadence_disposition="$initial_pre_disposition"
else
  if ! generation_control_resume; then
    echo "Could not durably resume generation control; refusing to start Codex." >&2
    exit 70
  fi
fi

elapsed_timer() {
  while true; do
    sleep "$TIMER_INTERVAL_SECONDS"
    local now
    now=$(date +%s)
    local secs=$((now - START_EPOCH))
    printf "\r  [wrapper elapsed %s; display only] still running..." "$(format_duration "$secs")"
  done
}

elapsed_timer &
TIMER_PID=$!

cleanup_timer() {
  kill "$TIMER_PID" 2>/dev/null || true
  wait "$TIMER_PID" 2>/dev/null || true
}
trap cleanup_timer EXIT

yielded_state=""
cadence_terminal_state=""
cadence_cycle_budget_exhausted=""
cadence_cycles_started=0
CADENCE_ROOT_INVOCATION_FAILSAFE=128
cadence_guard_cycle_id=""
cadence_root_invocations_in_cycle=0
iter=0
while true; do
  if [[ "$REVIEW_CADENCE_POLICY" == disabled \
     && "$iter" -ge "$MAX_ITERATIONS" ]]; then
    break
  fi
  log_file="$ACTIVE_LOG_DIR/${problem_name}_iter_${iter}.md"

  if ! trusted_runtime_unchanged; then
    echo "Trusted generation runtime changed; refusing to start another session." >&2
    exit 70
  fi

  if receipt_is_valid; then
    echo "Solved problem_id=$problem_rel before iter=$iter"
    break
  fi

  if [[ -n "$RETHLAS_HOTJOIN_RUN_ID" ]]; then
    current_hotjoin_sha256="$(
      "$TRUSTED_PYTHON_BIN" -I -B -c \
        'import hashlib, pathlib, sys; print(hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest())' \
        "$HOTJOIN_ADAPTER"
    )"
    if [[ "$current_hotjoin_sha256" != "$HOTJOIN_ADAPTER_SHA256" ]]; then
      echo "Hot-join adapter changed; refusing to start another session." >&2
      exit 70
    fi
    current_advisor_sha256="$(
      "$TRUSTED_PYTHON_BIN" -I -B -c \
        'import hashlib, pathlib, sys; print(hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest())' \
        "$ADVISOR_BRIDGE"
    )"
    if [[ "$current_advisor_sha256" != "$ADVISOR_BRIDGE_SHA256" ]]; then
      echo "Advisor bridge changed; refusing to start another session." >&2
      exit 70
    fi
  fi

  cadence_start_state=""
  cadence_continuation=0
  cadence_active_continuation=0
  cadence_reviewed_epoch_continuation=0
  cadence_recovery=0
  cadence_paid_root_invocation=0
  cadence_new_cycle_invocation=0
  cadence_start_cycle_id=""
  if [[ "$REVIEW_CADENCE_POLICY" != disabled ]]; then
    if [[ "$iter" -eq 0 ]]; then
      cadence_stage="initial"
    else
      cadence_stage="next"
    fi
    cadence_start_projection="$(cadence_control_projection)" || exit 70
    cadence_start_cycle_id="$(
      cadence_projection_cycle_id "$cadence_start_projection"
    )" || exit 70
    if ! cadence_start_state="$(
      cadence_start_disposition "$cadence_stage" "$cadence_start_projection"
    )"; then
      echo "Durable cadence state forbids iter=$iter; no paid turn was started." >&2
      exit 70
    fi
    if [[ "$cadence_start_state" == hard_stopped ]]; then
      cadence_terminal_state="$cadence_start_state"
      echo "Cadence is already at its finalized T+150m hard stop before iter=$iter; no recovery or additional paid cycle is authorized."
      break
    fi
    if [[ "$cadence_start_state" == route_frozen ]]; then
      cadence_terminal_state="$cadence_start_state"
      echo "The active route is durably frozen after an official red verdict with no authorized fallback before iter=$iter; no owner/advisor wait or paid continuation is authorized." >&2
      break
    fi
    if [[ "$cadence_start_state" == stop_unsolved ]]; then
      cadence_terminal_state="$cadence_start_state"
      echo "The continuous round is already durably stopped unsolved before iter=$iter; no paid successor is authorized." >&2
      break
    fi
    if [[ "$cadence_start_state" == initial_start_allowed \
       || "$cadence_start_state" == continue_next_cycle ]]; then
      if [[ "$cadence_cycles_started" -ge "$MAX_ITERATIONS" ]]; then
        cadence_cycle_budget_exhausted="$cadence_start_state"
        echo "Owner cycle budget MAX_ITERATIONS=$MAX_ITERATIONS is exhausted before disposition=$cadence_start_state; the authorized cycle was not consumed." >&2
        break
      fi
      cadence_cycles_started=$((cadence_cycles_started + 1))
    fi
    if [[ "$cadence_start_state" == continue_next_cycle ]]; then
      cadence_continuation=1
      cadence_paid_root_invocation=1
      cadence_new_cycle_invocation=1
    elif [[ "$cadence_start_state" == continue_active_cycle ]]; then
      cadence_active_continuation=1
      cadence_paid_root_invocation=1
    elif [[ "$cadence_start_state" == continue_reviewed_cycle_fresh_epoch ]]; then
      cadence_reviewed_epoch_continuation=1
      cadence_paid_root_invocation=1
    elif [[ "$cadence_start_state" == initial_start_allowed ]]; then
      cadence_paid_root_invocation=1
      cadence_new_cycle_invocation=1
    elif [[ "$cadence_start_state" == continuous_intent_successor_required \
         || "$cadence_start_state" == continuous_verdict_successor_required ]]; then
      cadence_paid_root_invocation=1
    elif [[ "$cadence_start_state" == resume_active_cycle \
         || "$cadence_start_state" == terminal_observed_pending_finalization \
         || "$cadence_start_state" == review_boundary_recovery_required \
         || "$cadence_start_state" == continuous_review_host_recovery ]]; then
      cadence_recovery=1
    fi
    if [[ "$cadence_paid_root_invocation" -eq 1 ]]; then
      if [[ "$cadence_new_cycle_invocation" -eq 1 ]]; then
        # The host creates the distinct cycle/T0 atomically inside the ensuing
        # adapter command. Keep the prior authenticated id so the post-call
        # projection must prove that a genuinely different cycle was created.
        cadence_prior_cycle_id="$cadence_guard_cycle_id"
        if [[ -z "$cadence_prior_cycle_id" ]]; then
          cadence_prior_cycle_id="$cadence_start_cycle_id"
        fi
        cadence_root_invocations_in_cycle=0
      else
        if [[ -z "$cadence_start_cycle_id" ]]; then
          echo "Paid same-cycle disposition lacks an authenticated cycle_id; refusing to start." >&2
          exit 70
        fi
        if [[ -z "$cadence_guard_cycle_id" ]]; then
          # A true wrapper restart has no local counter history. The durable
          # host authorization remains primary; bind this secondary guard to
          # its exact current cycle rather than treating restart as a new one.
          cadence_guard_cycle_id="$cadence_start_cycle_id"
          cadence_root_invocations_in_cycle=0
        elif [[ "$cadence_start_cycle_id" != "$cadence_guard_cycle_id" ]]; then
          echo "Durable cycle_id changed without initial_start_allowed or continue_next_cycle; refusing to reset the runner fail-safe." >&2
          exit 70
        fi
      fi
      if [[ "$cadence_root_invocations_in_cycle" -ge "$CADENCE_ROOT_INVOCATION_FAILSAFE" ]]; then
        echo "Cadence exceeded the runner's ${CADENCE_ROOT_INVOCATION_FAILSAFE}-paid-root operational fail-safe for cycle_id=${cadence_guard_cycle_id:-pending}; refusing another turn." >&2
        exit 70
      fi
      cadence_root_invocations_in_cycle=$((cadence_root_invocations_in_cycle + 1))
    fi
  fi

  if [[ "$REVIEW_CADENCE_POLICY" == rethlas_continuous_supervisor_v1 \
     && "$cadence_continuation" -eq 1 ]]; then
    prompt="Consume only the authenticated continuous owner-yield handoff for problem_id=${problem_rel} in its fresh root thread epoch. Resume the same logical supervisor and preserve its rolling 60-minute review clock, 150-minute nonterminal renewal clock, review history, and retired-lane set after the recorded owner-pause compensation. This transport admission is not a new mathematical run and does not revive a retired route. Read AGENTS.md and ${PROBLEM_FILE}; the adapter must replace this marker with the exact content-addressed handoff before turn/start."
    web_mode="disabled"
  elif [[ "$cadence_recovery" -eq 1 ]]; then
    prompt="Recover only the already-authorized durable scheduler operation for problem_id=${problem_rel} with disposition=${cadence_start_state}. Do not start a new paid turn or a new route cycle. Preserve the existing absolute cycle T0 and T+150m hard stop, finalize the observed terminal state when applicable, and obey review_cadence_policy=${REVIEW_CADENCE_POLICY} plus context_guard_policy=${CONTEXT_GUARD_POLICY}."
    web_mode="disabled"
  elif [[ "$cadence_start_state" == continuous_verdict_successor_required ]]; then
    prompt="Apply only the immutable host-completed continuous review verdict for problem_id=${problem_rel}. Start no new route before the adapter binds and applies that exact decision. This is one same-cycle successor, not a review retry or clock reset."
    web_mode="disabled"
  elif [[ "$cadence_start_state" == continuous_intent_successor_required ]]; then
    prompt="Consume only the one durable cause-bound continuous root intent for problem_id=${problem_rel}. The adapter must replace this marker with the exact immutable intent prompt before turn/start. A clean return, supervisor state, or elapsed time is not successor authority."
    web_mode="disabled"
  elif [[ "$cadence_active_continuation" -eq 1 ]]; then
    prompt="Continue only the scheduler-authorized active 150-minute route cycle for problem_id=${problem_rel} in its existing app-server thread epoch. Preserve the original absolute cycle T0 and the T+60m/T+120m cooperative review drains, the T+147m close boundary, and the T+150m hard stop; this clean prior turn terminal created one paid-turn authorization, not a new cycle or a clock reset. Obey the durable allowed action and context guard. If context rollover is required, stop same-thread work and use the authenticated handoff path."
    web_mode="disabled"
  elif [[ "$cadence_reviewed_epoch_continuation" -eq 1 ]]; then
    # The adapter must replace this marker with its host-generated canonical
    # rehydration prompt only after atomically consuming the exact pending
    # review handoff. Model-authored or runner-authored handoff text is not an
    # authority surface.
    prompt="[TRUSTED HOST REHYDRATION REQUIRED] Consume only the authenticated post-review handoff for problem_id=${problem_rel}; preserve the existing cycle T0 and absolute deadlines."
    web_mode="disabled"
  elif [[ "$cadence_continuation" -eq 1 ]]; then
    prompt="Begin only the scheduler-authorized next cycle for problem_id=${problem_rel} in the brand-new app-server thread epoch bound to the authenticated context handoff. Use reasoning_contract=rethlas_safe_three_route_v1, review_cadence_policy=${REVIEW_CADENCE_POLICY}, and context_guard_policy=${CONTEXT_GUARD_POLICY}. Rehydrate only the bounded handoff and authoritative local files; do not treat a transcript, automatic compaction, or this wrapper's local iteration counter as progress or as a deadline reset. This is a distinct host-created cycle with its own durable pre-dispatch T0 and new absolute review/close/hard-stop deadlines; it never resets or extends the already closed prior cycle. Read AGENTS.md and ${PROBLEM_FILE}; obey the persisted active route, effective review verdict, and allowed action. The trusted local runtime provides NumPy, SciPy, SymPy, mpmath, and gmpy2. Ignore any pre-existing blueprint_verified.md: only verify_blueprint_service and its trusted receipt can finish this run. If a complete candidate appears, enter the candidate fast lane immediately."
    if ((iter % 2 == 1)); then
      prompt="${prompt} Do not use arXiv theorem search or web search in this cycle."
      web_mode="disabled"
    else
      prompt="${prompt} External retrieval is a capability, not an obligation: use it only for one named knowledge gap under the two-query budget."
      web_mode="live"
    fi
  elif [[ "$iter" -eq 0 ]]; then
    prompt="Use AGENTS.md exactly with reasoning_contract=rethlas_safe_three_route_v1 to solve the math problem in ${PROBLEM_FILE}. Use problem_id=${problem_rel}. ${ref_prompt} A trusted math-research runtime is available as both python and python3, with NumPy, SciPy, SymPy, mpmath, and gmpy2 importable. This is iteration 0 in a fresh session. Begin with the protected root route-design phase, with ${DEEP_WORK_MINUTES} minutes as a soft target that must not delay a ready fanout: after reading the authoritative problem and local references, do not initialize or write memory, retrieve externally, update branches, or spawn collaborators until either a complete candidate exists or exactly three materially different, scope-disjoint routes have been screened for duplication, obvious contradiction, and basic viability. If no candidate exists, flush one pre-fanout checkpoint containing the exact three plan ids, mechanisms, scopes, discriminating tests, and exactly one provisional active rethlas_active_route_commitment_v1 for scheduled review. Then spawn exactly three context-free route solvers in one fanout. Children cannot spawn agents or write shared memory, and the root must not pursue a fourth proof route. Necessary local exact, symbolic, or numerical computation is allowed. Any complete candidate preempts the fanout or remaining waits and enters the candidate fast lane for verifier publication. Ignore any pre-existing blueprint_verified.md: only verify_blueprint_service and its trusted receipt can finish this run."
    if [[ "$REVIEW_CADENCE_POLICY" == rethlas_continuous_supervisor_v1 ]]; then
      prompt="${prompt} The trusted continuous supervisor protects the logical root, freezes and comparatively reviews at most three child lanes every rolling 60 minutes, parks completed child turns for resume/restart/retire decisions, and treats each 150-minute renewal as nonterminal. Never interpret a renewal as a hard stop or reset the global review clock. At an all-route dead end, prepare the evidence-bound advisor checkpoint and yield; otherwise remain available for the next cohort."
    elif [[ "$REVIEW_CADENCE_POLICY" == rethlas_route_review_150m_v2 ]]; then
      prompt="${prompt} The trusted owner-side scheduler has started the absolute 150-minute route cycle under review_cadence_policy=${REVIEW_CADENCE_POLICY} and context_guard_policy=${CONTEXT_GUARD_POLICY}; it, not this prompt or your estimate, enforces T+60m/T+120m cooperative review drains, T+147m close, context handoff, and the T+150m hard stop."
    fi
    web_mode="disabled"
  elif ((iter % 2 == 1)); then
    if [[ -n "$RETHLAS_HOTJOIN_RUN_ID" ]]; then
      session_instruction="Continue the persistent main conversation for problem_id=${problem_rel}."
    else
      session_instruction="Start a fresh reasoning session and continue problem_id=${problem_rel}."
    fi
    prompt="${session_instruction} Use reasoning_contract=rethlas_safe_three_route_v1. Read AGENTS.md, ${PROBLEM_FILE}, the current results/${problem_rel}/blueprint.md if it exists, and use at most one bounded memory_search only when essential state is missing from the active conversation. Continue the current three-route generation or, only after its three reports and shared failure synthesis are durable, design the next exact three-route fanout before any new checkpoint. The trusted local runtime still provides NumPy, SciPy, SymPy, mpmath, and gmpy2. Ignore any pre-existing blueprint_verified.md: only verify_blueprint_service and its trusted receipt can finish this run. This is iteration ${iter}. Do not use arXiv theorem search or web search. If a complete candidate appears, enter the candidate fast lane immediately."
    web_mode="disabled"
  else
    if [[ -n "$RETHLAS_HOTJOIN_RUN_ID" ]]; then
      session_instruction="Continue the persistent main conversation for problem_id=${problem_rel}."
    else
      session_instruction="Start a fresh reasoning session and continue problem_id=${problem_rel}."
    fi
    prompt="${session_instruction} Use reasoning_contract=rethlas_safe_three_route_v1. Read AGENTS.md, ${PROBLEM_FILE}, the current results/${problem_rel}/blueprint.md if it exists, and use at most one bounded memory_search only when essential state is missing from the active conversation. Continue the current three-route generation or, only after its three reports and shared failure synthesis are durable, design the next exact three-route fanout before any new checkpoint. The trusted local runtime still provides NumPy, SciPy, SymPy, mpmath, and gmpy2. Ignore any pre-existing blueprint_verified.md: only verify_blueprint_service and its trusted receipt can finish this run. This is iteration ${iter}. arXiv theorem search and web search are capabilities, not obligations: use them only for one named external knowledge gap under the two-query budget, then return to reasoning. If a complete candidate appears, enter the candidate fast lane immediately."
    web_mode="live"
  fi

  prompt="${prompt} The owner-resolved orchestration cost policy is ${RESOLVED_COST_GATE_POLICY}, bound by manifest SHA-256 ${RETHLAS_RESOLVED_COST_POLICY_SHA256}. Record that exact value in recursive-round telemetry. Under disabled_by_owner, cost telemetry cannot stop work or create waiting_cost_gate."

  if [[ -z "$RETHLAS_HOTJOIN_RUN_ID" ]]; then
    prompt="${prompt} This cadence-disabled legacy transport has no trusted owner-yield surface. On a truthful non-success, persist the mathematical failure and return unverified; do not write waiting_cost_gate or waiting_owner_advisor_decision, do not create an advisor checkpoint, and do not call generation_yield."
  fi

  guardian_admission_mode=""
  guardian_expected_cycle_id=""
  guardian_expected_generation=""
  guardian_expected_clock_sha256=""
  guardian_watchdog_id=""
  if [[ "$REVIEW_CADENCE_POLICY" != disabled ]]; then
    if ! guardian_plan="$(
      guardian_launch_plan "$cadence_start_projection" "$cadence_start_state"
    )"; then
      echo "Could not derive an exact Guardian admission for iter=$iter." >&2
      exit 70
    fi
    IFS=$'\t' read -r guardian_admission_mode guardian_expected_cycle_id \
      guardian_expected_generation guardian_expected_clock_sha256 \
      guardian_watchdog_id <<< "$guardian_plan"
    if [[ -z "$guardian_admission_mode" \
       || -z "$guardian_expected_cycle_id" \
       || -z "$guardian_expected_generation" \
       || -z "$guardian_expected_clock_sha256" \
       || -z "$guardian_watchdog_id" ]]; then
      echo "Guardian admission plan is incomplete; refusing iter=$iter." >&2
      exit 70
    fi
  fi

  if [[ -n "$RETHLAS_HOTJOIN_RUN_ID" ]]; then
    generator_command=(
      "$TRUSTED_PYTHON_BIN" "$HOTJOIN_ADAPTER"
      --db "$HOTJOIN_DB"
      run-generator
      --run-id "$RETHLAS_HOTJOIN_RUN_ID"
      --problem-id "$problem_rel"
      --cwd "$ROOT_DIR"
      --prompt "$prompt"
      --model "$MODEL"
      --effort "$REASONING_EFFORT"
      --web-mode "$web_mode"
      --mcp-config-toml "$TRUSTED_REASONING_MCP_BASE_TOML"
      --shell-policy-toml "$TRUSTED_SHELL_ENVIRONMENT_POLICY_TOML"
      --codex-bin "$CODEX_BIN"
      --codex-bin-sha256 "$CODEX_BIN_SHA256"
      --advisor-control-plane-sha256 "$ADVISOR_BRIDGE_SHA256"
      --review-cadence-policy "$REVIEW_CADENCE_POLICY"
      --context-guard-policy "$CONTEXT_GUARD_POLICY"
    )
    if [[ -n "$POLICY_CONTRACT_SHA256" ]]; then
      generator_command+=(
        --policy-contract-sha256 "$POLICY_CONTRACT_SHA256"
        --review-contract-cli-path "$REVIEW_CONTRACT_CLI_PATH"
        --review-contract-cli-sha256 "$REVIEW_CONTRACT_CLI_SHA256"
        --review-driver-path "$REVIEW_DRIVER_PATH"
        --review-driver-sha256 "$REVIEW_DRIVER_SHA256"
        --review-driver-package-sha256 "$REVIEW_DRIVER_PACKAGE_SHA256"
        --trusted-runtime-sha256 "$TRUSTED_RUNTIME_MANIFEST"
      )
    fi
    if [[ "$REVIEW_CADENCE_POLICY" == rethlas_continuous_supervisor_v1 ]]; then
      # Zero is an explicit continuous-only sentinel. The monitor-only Guardian,
      # owner yield, verifier receipt, and safety failures remain terminal
      # authorities; an arbitrary four-hour adapter timer does not.
      generator_command+=(--max-runtime-seconds 0)
    fi
  else
    generator_command=(
      "$CODEX_BIN" exec
      -C "$ROOT_DIR"
      -m "$MODEL"
      --config "model_reasoning_effort=\"$REASONING_EFFORT\""
      --config "agents.default_subagent_model=\"$MODEL\""
      --config "agents.default_subagent_reasoning_effort=\"$REASONING_EFFORT\""
      --config "web_search=\"$web_mode\""
      --config "shell_environment_policy=$TRUSTED_SHELL_ENVIRONMENT_POLICY_TOML"
      --config "mcp_servers.reasoning_agent=$TRUSTED_REASONING_AGENT_MCP_TOML"
      --config "mcp_servers.reasoning_checkpoint_primary=$TRUSTED_REASONING_CHECKPOINT_PRIMARY_MCP_TOML"
      --config "mcp_servers.reasoning_checkpoint_recovery=$TRUSTED_REASONING_CHECKPOINT_RECOVERY_MCP_TOML"
      --sandbox workspace-write
      --ephemeral
      "$prompt"
    )
  fi

  if [[ -n "$RETHLAS_HOTJOIN_RUN_ID" ]] \
     && ! hotjoin_control_sources_unchanged; then
    echo "Trusted control/helper/Codex sources changed before iter=$iter; zero root/reviewer turns were started." >&2
    exit 70
  fi

  if [[ "$REVIEW_CADENCE_POLICY" != disabled ]]; then
    guardian_command=(
      --db "$HOTJOIN_DB"
      --adapter-path "$HOTJOIN_ADAPTER"
      --adapter-sha256 "$HOTJOIN_ADAPTER_SHA256"
      --guardian-path "$GUARDIAN_SOURCE"
      --runner-path "$GUARDIAN_RUNNER"
      --run-id "$RETHLAS_HOTJOIN_RUN_ID"
      --generation-control-instance-id "$RETHLAS_GENERATION_CONTROL_TOKEN"
      --watchdog-id "$guardian_watchdog_id"
      --admission-mode "$guardian_admission_mode"
      --expected-cycle-id "$guardian_expected_cycle_id"
      --expected-generation "$guardian_expected_generation"
      --capability-revision "$CONTROL_CAPABILITY_REVISION"
      --policy-contract-sha256 "$POLICY_CONTRACT_SHA256"
      --policy-digest "$REVIEW_POLICY_SHA256"
      --guardian-mode "$([[ "$REVIEW_CADENCE_POLICY" == rethlas_continuous_supervisor_v1 ]] && printf monitor_only || printf hard_stop)"
      --worker-cwd "$ROOT_DIR"
      --problem-path "$ROOT_DIR/$PROBLEM_FILE"
      --problem-relative-path "$PROBLEM_FILE"
      --worker-mode runner_control
    )
    if [[ "$guardian_expected_clock_sha256" != "-" ]]; then
      guardian_command+=(
        --expected-clock-sha256 "$guardian_expected_clock_sha256"
      )
    fi
    guardian_command+=(-- "${generator_command[@]}")
    if ! reserve_guarded_log "$log_file"; then
      echo "Could not reserve a fresh guarded log for iter=$iter." >&2
      exit 70
    fi
    echo "Starting guarded iter=$iter -> $log_file"
    if (
      cd "$ROOT_DIR"
      run_guardian_launcher "${guardian_command[@]}"
    ) >>"$log_file" 2>&1; then
      codex_rc=0
    else
      codex_rc=$?
    fi
  else
    echo "Starting iter=$iter -> $log_file"
    if (
      cd "$ROOT_DIR"
      "${generator_command[@]}"
    ) >"$log_file" 2>&1; then
      codex_rc=0
    else
      codex_rc=$?
    fi
  fi

  if ! trusted_runtime_unchanged; then
    echo "Trusted generation runtime was modified during iter=$iter; refusing to continue or accept publication." >&2
    exit 70
  fi

  if [[ -n "$RETHLAS_HOTJOIN_RUN_ID" ]]; then
    if ! hotjoin_control_sources_unchanged; then
      echo "Trusted Guardian/control/helper/Codex sources changed during iter=$iter; refusing to continue." >&2
      exit 70
    fi
    current_hotjoin_sha256="$(
      "$TRUSTED_PYTHON_BIN" -I -B -c \
        'import hashlib, pathlib, sys; print(hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest())' \
        "$HOTJOIN_ADAPTER"
    )"
    if [[ "$current_hotjoin_sha256" != "$HOTJOIN_ADAPTER_SHA256" ]]; then
      echo "Hot-join adapter was modified during iter=$iter; refusing to continue." >&2
      exit 70
    fi
    current_advisor_sha256="$(
      "$TRUSTED_PYTHON_BIN" -I -B -c \
        'import hashlib, pathlib, sys; print(hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest())' \
        "$ADVISOR_BRIDGE"
    )"
    if [[ "$current_advisor_sha256" != "$ADVISOR_BRIDGE_SHA256" ]]; then
      echo "Advisor bridge was modified during iter=$iter; refusing to continue." >&2
      exit 70
    fi
    if ! codex_executable_unchanged; then
      echo "Codex executable was modified during iter=$iter; refusing to continue or infer a terminal state." >&2
      exit 70
    fi
  fi

  # Read and authenticate generation control before asking cadence to infer any
  # follow-on authority. A clean app-server terminal is not by itself a paid
  # continuation, and a reviewer red is not an owner yield.
  current_generation_receipt="$(generation_control_receipt)" || exit 70
  current_generation_state="$(
    generation_control_state_from_receipt "$current_generation_receipt"
  )" || exit 70
  verified_after_turn=0
  if receipt_is_valid; then
    verified_after_turn=1
  fi
  cadence_after_projection=""
  cadence_after_turn=""
  cadence_after_cycle_id=""
  if [[ "$REVIEW_CADENCE_POLICY" == rethlas_route_review_150m_v2 ]]; then
    cadence_after_projection="$(cadence_control_projection)" || exit 70
    cadence_after_cycle_id="$(
      cadence_projection_cycle_id "$cadence_after_projection"
    )" || exit 70
    cadence_after_turn="$(
      cadence_projection_disposition "$cadence_after_projection"
    )" || exit 70
    if [[ "$codex_rc" -eq 0 && "$cadence_paid_root_invocation" -eq 1 ]]; then
      if [[ "$cadence_new_cycle_invocation" -eq 1 ]]; then
        if [[ -z "$cadence_after_cycle_id" \
           || ( -n "$cadence_prior_cycle_id" \
                && "$cadence_after_cycle_id" == "$cadence_prior_cycle_id" ) ]]; then
          echo "Paid new-cycle disposition did not establish a distinct authenticated cycle_id; refusing its result." >&2
          exit 70
        fi
        cadence_guard_cycle_id="$cadence_after_cycle_id"
      elif [[ "$cadence_after_cycle_id" != "$cadence_guard_cycle_id" ]]; then
        echo "Authenticated cycle_id changed during a same-cycle paid invocation; refusing its result." >&2
        exit 70
      fi
    fi
  elif [[ "$REVIEW_CADENCE_POLICY" == rethlas_continuous_supervisor_v1 ]]; then
    cadence_after_projection="$(cadence_control_projection)" || exit 70
    cadence_after_cycle_id="$(
      cadence_projection_cycle_id "$cadence_after_projection"
    )" || exit 70
    cadence_after_turn="$(
      cadence_projection_disposition "$cadence_after_projection"
    )" || exit 70
  fi

  if [[ "$codex_rc" -ne 0 ]]; then
    if [[ -n "$cadence_after_turn" ]]; then
      echo "Cadence disposition after failed iter=$iter: $cadence_after_turn" >&2
    fi
    echo "generator exited with code $codex_rc at iter=$iter (see $log_file for details)" >&2
    exit "$codex_rc"
  fi
  if [[ "$verified_after_turn" -eq 1 ]]; then
    if [[ "$REVIEW_CADENCE_POLICY" == rethlas_continuous_supervisor_v1 ]]; then
      cadence_close_verified_completion || exit 70
    fi
    echo "Solved problem_id=$problem_rel after iter=$iter"
    break
  fi

  if [[ "$REVIEW_CADENCE_POLICY" == rethlas_route_review_150m_v2 ]]; then
    case "$current_generation_state" in
      running)
        case "$cadence_after_turn" in
          continuation_authorization_required)
            cadence_admit continue_active_cycle "$current_generation_receipt" >/dev/null \
              || exit 70
            cadence_after_projection="$(cadence_control_projection)" || exit 70
            cadence_after_turn="$(
              cadence_projection_disposition "$cadence_after_projection"
            )" || exit 70
            ;;
          review_turn_authorization_required)
            echo "A due route review cannot be run inside an ordinary full-capability generator continuation; trusted host review orchestration is required." >&2
            exit 70
            ;;
          review_drive_required)
            REVIEW_DRIVE_PROJECTION=""
            if ! review_drive_due "$cadence_after_projection"; then
              exit 70
            fi
            cadence_after_projection="$REVIEW_DRIVE_PROJECTION"
            cadence_after_turn="$(
              cadence_projection_disposition "$cadence_after_projection"
            )" || exit 70
            if [[ "$cadence_after_turn" == continue_reviewed_cycle_fresh_epoch ]]; then
              echo "The due route review closed under trusted host orchestration after iter=$iter; its host-prepared same-cycle fresh epoch is ready." >&2
            elif [[ "$cadence_after_turn" == route_frozen ]]; then
              echo "The official review froze the active route after red with no authorized fallback after iter=$iter; no owner/advisor wait or paid continuation was created." >&2
            else
              echo "Trusted host review orchestration ended in state=$cadence_after_turn; no root turn is authorized." >&2
              exit 70
            fi
            ;;
        esac
        ;;
      waiting_cost_gate|waiting_owner_advisor_decision)
        cadence_close_owner_yield \
          "$current_generation_receipt" "$cadence_after_projection" >/dev/null \
          || exit 70
        cadence_after_projection="$(cadence_control_projection)" || exit 70
        cadence_after_turn="$(
          cadence_projection_disposition "$cadence_after_projection"
        )" || exit 70
        if [[ "$current_generation_state" == waiting_cost_gate \
           && "$cadence_after_turn" != owner_wait_cost ]]; then
          echo "Authenticated cost-gate yield did not close to owner_wait_cost." >&2
          exit 70
        fi
        if [[ "$current_generation_state" == waiting_owner_advisor_decision \
           && "$cadence_after_turn" != owner_wait_advisor ]]; then
          echo "Authenticated advisor yield did not close to owner_wait_advisor." >&2
          exit 70
        fi
        ;;
    esac
  elif [[ "$REVIEW_CADENCE_POLICY" == rethlas_continuous_supervisor_v1 ]]; then
    case "$current_generation_state" in
      waiting_cost_gate|waiting_owner_advisor_decision)
        cadence_close_owner_yield \
          "$current_generation_receipt" "$cadence_after_projection" >/dev/null \
          || exit 70
        cadence_after_projection="$(cadence_control_projection)" || exit 70
        cadence_after_turn="$(
          cadence_projection_disposition "$cadence_after_projection"
        )" || exit 70
        if [[ "$current_generation_state" == waiting_cost_gate \
           && "$cadence_after_turn" != owner_wait_cost ]]; then
          echo "Continuous cost-gate yield did not close to owner_wait_cost." >&2
          exit 70
        fi
        if [[ "$current_generation_state" == waiting_owner_advisor_decision \
           && "$cadence_after_turn" != owner_wait_advisor ]]; then
          echo "Continuous advisor yield did not close to owner_wait_advisor." >&2
          exit 70
        fi
        ;;
      running)
        if [[ "$cadence_after_turn" == stop_unsolved ]]; then
          cadence_terminal_state="$cadence_after_turn"
          echo "Continuous round ended with a durable stop_unsolved disposition after iter=$iter; no paid successor is authorized." >&2
          break
        fi
        if [[ "$cadence_after_turn" == continuous_active \
           || "$cadence_after_turn" == continuous_root_missing \
           || "$cadence_after_turn" == continuous_start_in_progress ]]; then
          echo "Continuous run-generator returned while the logical supervisor was nonterminal (state=$cadence_after_turn); refusing an unbound wrapper restart." >&2
          exit 70
        fi
        echo "Continuous supervisor ended without a verified receipt or legal owner yield (state=$cadence_after_turn)." >&2
        exit 70
        ;;
    esac
  fi

  case "$current_generation_state" in
    running)
      if [[ "$REVIEW_CADENCE_POLICY" == rethlas_route_review_150m_v2 ]]; then
        case "$cadence_after_turn" in
          continue_active_cycle|continue_next_cycle|continue_reviewed_cycle_fresh_epoch)
            ;;
          resume_active_cycle|terminal_observed_pending_finalization|review_boundary_recovery_required)
            if [[ "$cadence_recovery" -eq 1 ]]; then
              echo "Cadence recovery did not finalize disposition=$cadence_after_turn; refusing another recovery or paid turn." >&2
              exit 70
            fi
            ;;
          hard_stopped)
            cadence_terminal_state="$cadence_after_turn"
            echo "Cadence reached its finalized T+150m hard stop after iter=$iter; no additional paid cycle is authorized."
            break
            ;;
          route_frozen)
            cadence_terminal_state="$cadence_after_turn"
            echo "The active route is durably frozen after an official red verdict with no authorized fallback after iter=$iter; no owner/advisor wait or paid continuation is authorized." >&2
            break
            ;;
          *)
            echo "Cadence closed iter=$iter with disposition=$cadence_after_turn; refusing to infer permission for another paid cycle." >&2
            exit 70
            ;;
        esac
      fi
      ;;
    waiting_cost_gate|waiting_owner_advisor_decision)
      yielded_state="$current_generation_state"
      echo "Yielded unfinished problem_id=$problem_rel state=$yielded_state after iter=$iter; owner action is required before another paid turn."
      break
      ;;
    *)
      echo "Invalid durable generation control state after iter=$iter: $current_generation_state" >&2
      exit 70
      ;;
  esac

  echo "Finished problem_id=$problem_rel iter=$iter -> $log_file"
  iter=$((iter + 1))
done

cleanup_timer
trap - EXIT

END_EPOCH=$(date +%s)
TOTAL=$((END_EPOCH - START_EPOCH))
printf "\n"

print_total_time() {
  if [[ "$REVIEW_CADENCE_POLICY" != disabled ]]; then
    printf "Wrapper elapsed (display only): %s\n" "$(format_duration "$TOTAL")"
  else
    printf "Total time: %s\n" "$(format_duration "$TOTAL")"
  fi
}

if receipt_is_valid; then
  echo "Solved problem_id=$problem_rel -> $verified_path"
  print_total_time
  echo ""
  echo "To view results in the browser, run:"
  echo "  ./site/serve.sh"
  echo "Then open http://localhost:3264"
  exit 0
fi

if [[ -n "$yielded_state" ]]; then
  echo "The theorem remains unsolved; generation stopped at state=$yielded_state."
  print_total_time
  exit 0
fi

if [[ -n "$cadence_terminal_state" ]]; then
  echo "The theorem remains unsolved; durable cadence stopped at state=$cadence_terminal_state." >&2
  print_total_time
  exit 1
fi

if [[ -n "$cadence_cycle_budget_exhausted" ]]; then
  echo "The theorem remains unsolved; owner cycle budget stopped before state=$cadence_cycle_budget_exhausted." >&2
  print_total_time
  exit 1
fi

echo "Reached MAX_ITERATIONS=$MAX_ITERATIONS without verified blueprint for problem_id=$problem_rel" >&2
print_total_time
exit 1
